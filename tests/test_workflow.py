import io
import json
import zipfile
import pytest
from fastapi.testclient import TestClient
from app import config, db, ai, worker
from app.main import app, attempts


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB', tmp_path / 'test.db')
    monkeypatch.setattr(config, 'UPLOADS', tmp_path / 'uploads')
    config.UPLOADS.mkdir()
    monkeypatch.setattr(config, 'AI_MODE', 'demo')
    attempts.clear()
    db.init()
    # No lifespan: worker is advanced explicitly for deterministic assertions.
    with TestClient(app, headers={'X-Studio-Request': '1'}) as c:
        yield c


def register(c, email='family@example.com'):
    r = c.post('/api/register', json={'email': email, 'password': 'a-long-password', 'name': '家属'})
    assert r.status_code == 200, r.text
    return r.json()


def project(c):
    register(c)
    r = c.post('/api/projects', json={'name': '陈奶奶', 'title': '我的一生', 'consent': True})
    assert r.status_code == 200
    return r.json()['id']


def text_source(c, pid):
    r = c.post(f'/api/projects/{pid}/sources', json={'name': '故乡', 'text': '我在江边长大，母亲每天带我走过石桥。'})
    assert r.status_code == 200
    sid = r.json()['id']
    r = c.put(f'/api/projects/{pid}/sources/{sid}', json={'name': '故乡', 'text': '我在江边长大，母亲每天带我走过石桥。', 'confirmed': True})
    assert r.status_code == 200
    return sid


def finish_jobs(c, pid):
    import time
    for _ in range(200):
        worker.process_one()
        jobs = c.get(f'/api/projects/{pid}').json()['jobs']
        if all(j['status'] not in ('queued', 'running') for j in jobs):
            return jobs
        time.sleep(.02)
    raise AssertionError('jobs did not complete')


def test_offline_to_delivery_and_stale_revision(client):
    pid = project(client)
    assert client.get(f'/api/projects/{pid}/template').status_code == 200
    assert client.post(f'/api/projects/{pid}/generate').status_code == 409
    sid = text_source(client, pid)
    assert client.post(f'/api/projects/{pid}/generate').status_code == 200
    jobs = finish_jobs(client, pid)
    assert jobs[0]['status'] == 'done'
    p = client.get(f'/api/projects/{pid}').json()
    book = p['books'][0]
    ch = book['chapters'][0]
    assert ch['body'] == p['sources'][0]['text']
    assert client.get(f'/api/projects/{pid}/export?book_id={book["id"]}&draft=false').status_code == 409
    # Unresolved questions must block approval, even with a direct API request.
    data = {'expected_revision': ch['edit_revision'], 'title': ch['title'], 'body': ch['body'], 'questions': ['哪一年？'], 'approved': True}
    assert client.put(f'/api/projects/{pid}/chapters/{ch["id"]}', json=data).status_code == 409
    data['questions'] = []
    assert client.put(f'/api/projects/{pid}/chapters/{ch["id"]}', json=data).status_code == 200
    assert client.post(f'/api/projects/{pid}/books/{book["id"]}/release', json={'expected_content_sha256': client.get(f'/api/projects/{pid}').json()['books'][0]['content_sha256'], 'scope': 'family', 'signer': '陈奶奶', 'relationship': 'self', 'evidence_note': '当面逐章读过并确认家庭留存', 'confirmed': True}).status_code == 200
    result = client.get(f'/api/projects/{pid}/export?book_id={book["id"]}&format=zip&draft=false')
    assert result.status_code == 200
    with zipfile.ZipFile(io.BytesIO(result.content)) as z:
        assert 'book.docx' in z.namelist()
        assert '母亲' in z.read('book.md').decode()
        with zipfile.ZipFile(io.BytesIO(z.read('book.docx'))) as word:
            assert 'word/document.xml' in word.namelist()
    # Changed source invalidates the approved book.
    client.put(f'/api/projects/{pid}/sources/{sid}', json={'name': '故乡', 'text': '我在山村长大。', 'confirmed': True})
    assert client.get(f'/api/projects/{pid}/export?book_id={book["id"]}&format=zip').status_code == 409
    assert client.post(f'/api/projects/{pid}/generate').status_code == 200
    finish_jobs(client, pid)
    assert len(client.get(f'/api/projects/{pid}').json()['books']) == 2


def test_online_answers_persist_and_pause(client):
    pid = project(client)
    r = client.post(f'/api/projects/{pid}/messages', json={'text': '我的第一份工作是乡村教师。'})
    assert r.status_code == 200
    finish_jobs(client, pid)
    p = client.get(f'/api/projects/{pid}').json()
    assert p['messages'][-1]['role'] == 'assistant'
    assert '乡村教师' in p['sources'][0]['text']
    assert not p['sources'][0]['confirmed']
    assert client.post(f'/api/projects/{pid}/messages', json={'text': '休息'}).status_code == 200
    p = client.get(f'/api/projects/{pid}').json()
    assert '先休息' in p['messages'][-1]['content']
    assert '讲述者：休息' not in p['sources'][0]['text']


def test_tenant_isolation_and_editor_permissions(client):
    owner = register(client)
    pid = client.post('/api/projects', json={'name': '父亲', 'title': '往事', 'consent': True}).json()['id']
    sid = text_source(client, pid)
    client.post('/api/logout')
    editor = register(client, 'editor@example.com')
    for path in (f'/api/projects/{pid}', f'/api/projects/{pid}/template', f'/api/projects/{pid}/sources/{sid}/audio'):
        assert client.get(path).status_code == 404
    assert client.delete(f'/api/projects/{pid}').status_code == 404
    assert client.get('/api/projects').json() == []
    editor_cookie = client.cookies.get('studio_session')
    # logout revokes owner session, so log in again.
    client.post('/api/login', json={'email': 'family@example.com', 'password': 'a-long-password'})
    client.post(f'/api/projects/{pid}/members', json={'email': 'editor@example.com'})
    client.cookies.clear()
    client.cookies.set('studio_session', editor_cookie)
    assert client.get(f'/api/projects/{pid}').status_code == 200
    assert client.delete(f'/api/projects/{pid}').status_code == 404
    assert client.post(f'/api/projects/{pid}/members', json={'email': owner['email']}).status_code == 404
    client.post('/api/login', json={'email': 'family@example.com', 'password': 'a-long-password'})
    assert client.delete(f'/api/projects/{pid}/members/{editor["id"]}').status_code == 200
    client.cookies.clear()
    client.cookies.set('studio_session', editor_cookie)
    assert client.get(f'/api/projects/{pid}').status_code == 404


def test_audio_failure_manual_recovery_and_delete(client):
    pid = project(client)
    assert client.post(f'/api/projects/{pid}/audio', files={'file': ('evil.html', b'bad')}).status_code == 422
    r = client.post(f'/api/projects/{pid}/audio', files={'file': ('voice.wav', b'fake-audio', 'audio/wav')})
    assert r.status_code == 200
    sid = r.json()['id']
    jobs = finish_jobs(client, pid)
    assert jobs[0]['status'] == 'failed'
    assert '演示模式' in jobs[0]['error']
    assert client.get(f'/api/projects/{pid}/sources/{sid}/audio').content == b'fake-audio'
    assert client.put(f'/api/projects/{pid}/sources/{sid}', json={'name': '人工听写', 'text': '我记得小时候在田里帮忙。', 'confirmed': True}).status_code == 200
    assert client.post(f'/api/projects/{pid}/jobs/{r.json()["job_id"]}/retry').status_code == 409
    assert client.delete(f'/api/projects/{pid}').status_code == 200
    assert list(config.UPLOADS.iterdir()) == []
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM sources').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0


def test_upload_limit_and_csrf(client, monkeypatch):
    pid = project(client)
    monkeypatch.setattr(config, 'MAX_UPLOAD', 10)
    assert client.post(f'/api/projects/{pid}/audio', files={'file': ('a.wav', b'x'*11)}).status_code == 413
    assert list(config.UPLOADS.iterdir()) == []
    assert client.post('/api/logout', headers={'origin': 'https://attacker.example'}).status_code == 403
    assert client.get('/').headers['X-Frame-Options'] == 'DENY'
    assert client.get('/static/app.js').status_code == 200


def test_live_generation_validates_model_output(monkeypatch):
    monkeypatch.setattr(config, 'AI_MODE', 'live')
    monkeypatch.setattr(ai, 'chat', lambda *args: '{"title":"童年","body":"我在故乡长大。","questions":[]}')
    result = ai.write_chapter({'name': '老人', 'style': '质朴亲切'}, {'id': 'real-source', 'text': '我在故乡长大。'})
    assert result['source_ids'] == ['real-source']
    monkeypatch.setattr(ai, 'chat', lambda *args: '{"title":12,"body":[]}')
    with pytest.raises(ai.AIError):
        ai.write_chapter({'name': '老人', 'style': '质朴亲切'}, {'id': 'real-source', 'text': '真实素材'})


def test_provider_failure_keeps_user_answer(client, monkeypatch):
    pid = project(client)
    def fail(*args):
        raise ai.AIError('测试供应商不可用')
    monkeypatch.setattr(ai, 'followup', fail)
    response = client.post(f'/api/projects/{pid}/messages', json={'text': '我记得老家的竹林。'})
    finish_jobs(client, pid)
    p = client.get(f'/api/projects/{pid}').json()
    assert p['messages'][-1]['content'] == '我记得老家的竹林。'
    assert '竹林' in p['sources'][0]['text']
    assert p['jobs'][0]['status'] == 'failed'
    monkeypatch.setattr(ai, 'followup', lambda *args: '那片竹林在什么地方？')
    assert client.post(f'/api/projects/{pid}/jobs/{response.json()["job_id"]}/retry').status_code == 200
    finish_jobs(client, pid)
    assert client.get(f'/api/projects/{pid}').json()['messages'][-1]['content'] == '那片竹林在什么地方？'


def test_online_audio_reuses_source_without_duplicate(client):
    pid = project(client)
    r = client.post(f'/api/projects/{pid}/audio', files={'file': ('voice.wav', b'fake-audio')})
    sid = r.json()['id']
    finish_jobs(client, pid)
    client.put(f'/api/projects/{pid}/sources/{sid}', json={'name': '在线录音', 'text': '我出生在江边。', 'confirmed': False})
    assert client.post(f'/api/projects/{pid}/messages', json={'text': '我出生在江边。', 'source_id': sid}).status_code == 200
    finish_jobs(client, pid)
    p = client.get(f'/api/projects/{pid}').json()
    assert len(p['sources']) == 1
    assert '采访者：' in p['sources'][0]['text']
    assert '讲述者：我出生在江边。' in p['sources'][0]['text']


def test_live_chat_http_contract(monkeypatch):
    import httpx
    original_client = httpx.Client
    seen = []
    def handle(request):
        seen.append(request)
        assert request.url.path == '/v1/chat/completions'
        assert request.headers['authorization'] == 'Bearer test-only-key'
        payload = json.loads(request.content)
        assert payload['model'] == 'test-model'
        assert payload['response_format'] == {'type': 'json_object'}
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"title":"往事","body":"我的故事","questions":[]}'}}]})
    monkeypatch.setenv('TEXT_BASE_URL', 'https://test.invalid/v1')
    monkeypatch.setenv('TEXT_API_KEY', 'test-only-key')
    monkeypatch.setenv('TEXT_MODEL', 'test-model')
    monkeypatch.setattr(ai.httpx, 'Client', lambda **kw: original_client(transport=httpx.MockTransport(handle), **kw))
    assert json.loads(ai.chat('测试', '素材', True))['title'] == '往事'
    assert len(seen) == 1


def test_live_asr_chunk_contract(tmp_path, monkeypatch):
    import httpx
    import subprocess
    original_client = httpx.Client
    monkeypatch.setattr(config, 'AI_MODE', 'live')
    monkeypatch.setenv('ASR_BASE_URL', 'https://test.invalid/v1')
    monkeypatch.setenv('ASR_API_KEY', 'asr-test-key')
    monkeypatch.setenv('ASR_MODEL', 'asr-test-model')
    monkeypatch.setattr(ai.shutil, 'which', lambda name: '/fake/' + name)
    def run(args, **kw):
        if args[0] == 'ffprobe':
            return subprocess.CompletedProcess(args, 0, stdout='{"format":{"duration":"720"}}')
        pattern = args[-1]
        from pathlib import Path
        Path(pattern.replace('%04d', '0000')).write_bytes(b'RIFF-test-chunk-one')
        Path(pattern.replace('%04d', '0001')).write_bytes(b'RIFF-test-chunk-two')
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(ai.subprocess, 'run', run)
    def handle(request):
        assert request.url.path == '/v1/audio/transcriptions'
        assert b'asr-test-model' in request.content
        assert b'RIFF-test-chunk' in request.content
        return httpx.Response(200, json={'text': '这是讲述者的真实回忆。'})
    monkeypatch.setattr(ai.httpx, 'Client', lambda **kw: original_client(transport=httpx.MockTransport(handle), **kw))
    updates = []
    text, segments = ai.transcribe(tmp_path / 'input.mp3', updates.append)
    assert len(segments) == 2
    assert segments[1]['start'] == 600 and segments[1]['end'] == 720
    assert text.count('真实回忆') == 2
    assert len(updates) == 2
