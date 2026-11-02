import json

import pytest
from fastapi.testclient import TestClient

from app import ai, config, db, editorial
from app.main import app, attempts
from app.runtime import JobCancelled


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB', tmp_path / 'editorial.sqlite3')
    monkeypatch.setattr(config, 'UPLOADS', tmp_path / 'uploads')
    config.UPLOADS.mkdir()
    monkeypatch.setattr(config, 'AI_MODE', 'demo')
    attempts.clear()
    db.init()
    c = TestClient(app, headers={'X-Studio-Request': '1'})
    c.post('/api/register', json={'email': 'author@example.com', 'password': 'long-test-password', 'name': '家人'})
    yield c
    c.close()


def setup_project(c, texts=('我在江边长大。', '后来我到城里工作。')):
    pid = c.post('/api/projects', json={'name': '讲述者', 'title': '我的岁月', 'consent': True}).json()['id']
    ids = []
    for i, text in enumerate(texts):
        data = {'name': f'访谈{i+1}', 'text': text}
        sid = c.post(f'/api/projects/{pid}/sources', json=data).json()['id']
        assert c.put(f'/api/projects/{pid}/sources/{sid}', json={**data, 'confirmed': True}).status_code == 200
        ids.append(sid)
    return pid, ids


def run(jid):
    with db.connect() as c:
        c.execute("UPDATE jobs SET status='running' WHERE id=?", (jid,))
        job = dict(c.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone())
    editorial.run_job(job, json.loads(job['payload']), lambda _: None)


def plan(c, pid):
    response = c.post(f'/api/projects/{pid}/editorial/plan')
    assert response.status_code == 200, response.text
    run(response.json()['job_id'])
    return c.get(f'/api/projects/{pid}/editorial').json()['plans'][0]


def approve_plan(c, pid, p):
    for fact in p['facts']:
        fact['status'] = 'confirmed'
    data = {'expected_revision': p['edit_revision'], 'facts': p['facts'], 'chapters': p['chapters'], 'confirmed': True}
    response = c.put(f'/api/projects/{pid}/editorial/plans/{p["id"]}', json=data)
    assert response.status_code == 200, response.text


def draft(c, pid, p):
    response = c.post(f'/api/projects/{pid}/editorial/plans/{p["id"]}/draft')
    assert response.status_code == 200, response.text
    run(response.json()['job_id'])
    return c.get(f'/api/projects/{pid}').json()['books'][0]


def test_demo_extracts_all_chunks_and_validates_exact_quotes(client):
    text = '起' + '中' * 12001 + '尾'
    pid, ids = setup_project(client, (text,))
    p = plan(client, pid)
    assert ''.join(f['quote'] for f in p['facts']) == text
    assert all(f['status'] == 'uncertain' for f in p['facts'])
    assert p['facts'][-1]['end'] == len(text)
    p['facts'][0]['quote'] = '这句话不存在'
    response = client.put(f'/api/projects/{pid}/editorial/plans/{p["id"]}', json={
        'expected_revision': p['edit_revision'], 'facts': p['facts'], 'chapters': p['chapters'], 'confirmed': False})
    assert response.status_code == 422


def test_live_fact_extractor_rejects_invented_quote(monkeypatch):
    monkeypatch.setattr(config, 'AI_MODE', 'live')
    monkeypatch.setattr(editorial, 'structured', lambda *args: {'facts': [{'people': '我', 'time': '', 'event': '童年', 'quote': '模型捏造的原话'}]})
    with pytest.raises(ai.AIError, match='引文与原文不符'):
        editorial.propose_plan({'sources': [{'id': 's', 'name': '采访', 'text': '我在江边长大。'}]}, lambda _: None)


def test_outline_requires_all_sources_and_confirmed_facts(client):
    pid, ids = setup_project(client)
    p = plan(client, pid)
    body = {'expected_revision': 0, 'facts': p['facts'], 'chapters': p['chapters'], 'confirmed': True}
    assert client.put(f'/api/projects/{pid}/editorial/plans/{p["id"]}', json=body).status_code == 409
    body['confirmed'] = False
    body['chapters'] = body['chapters'][:1]
    assert client.put(f'/api/projects/{pid}/editorial/plans/{p["id"]}', json=body).status_code == 422
    assert client.post(f'/api/projects/{pid}/editorial/plans/{p["id"]}/draft').status_code == 409


def test_confirmed_outline_groups_multiple_sources_and_stale_plan_rejected(client):
    pid, ids = setup_project(client)
    p = plan(client, pid)
    p['chapters'] = [{**p['chapters'][0], 'title': '从故乡到城市', 'source_ids': ids}]
    approve_plan(client, pid, p)
    book = draft(client, pid, p)
    assert len(book['chapters']) == 1
    ch = book['chapters'][0]
    assert '江边' in ch['body'] and '城里' in ch['body']
    assert json.loads(ch['source_ids']) == ids
    assert not ch['approved']
    client.put(f'/api/projects/{pid}/sources/{ids[0]}', json={'name': '更正', 'text': '我在山村长大。', 'confirmed': True})
    assert client.post(f'/api/projects/{pid}/editorial/plans/{p["id"]}/draft').status_code == 409


def test_cross_chapter_check_adds_questions_without_rewriting(client):
    pid, _ = setup_project(client, ('我于1950年开始上学。', '我于1951年开始上学。'))
    p = plan(client, pid)
    approve_plan(client, pid, p)
    book = draft(client, pid, p)
    originals = {ch['id']: ch['body'] for ch in book['chapters']}
    response = client.post(f'/api/projects/{pid}/editorial/books/{book["id"]}/check')
    assert response.status_code == 200
    run(response.json()['job_id'])
    updated = client.get(f'/api/projects/{pid}').json()['books'][0]
    for ch in updated['chapters']:
        assert ch['body'] == originals[ch['id']]
        assert any('年份不同' in q for q in json.loads(ch['questions']))
        assert not ch['approved']
    assert client.get(f'/api/projects/{pid}/editorial').json()['checks'][0]['issues']


def test_check_rejects_fabricated_evidence(monkeypatch):
    monkeypatch.setattr(config, 'AI_MODE', 'live')
    monkeypatch.setattr(editorial, 'structured', lambda *args: {'issues': [{'question': '是否矛盾？', 'evidence': [
        {'chapter_id': 'a', 'quote': '捏造'}, {'chapter_id': 'b', 'quote': '第二章'}]}]})
    with pytest.raises(ai.AIError, match='审查引文'):
        editorial.check_book({'chapters': [{'id': 'a', 'body': '第一章'}, {'id': 'b', 'body': '第二章'}]}, lambda _: None)


def test_merge_keeps_original_version_and_history_restore(client):
    pid, _ = setup_project(client)
    p = plan(client, pid)
    approve_plan(client, pid, p)
    book = draft(client, pid, p)
    merged = client.post(f'/api/projects/{pid}/editorial/books/{book["id"]}/merge', json={
        'title': '人生前半程', 'chapter_ids': [ch['id'] for ch in book['chapters']],
        'expected_content_sha256': book['content_sha256']})
    assert merged.status_code == 200, merged.text
    books = client.get(f'/api/projects/{pid}').json()['books']
    assert len(books) == 2 and len(books[0]['chapters']) == 1 and len(books[1]['chapters']) == 2
    ch = books[0]['chapters'][0]
    original = ch['body']
    response = client.put(f'/api/projects/{pid}/chapters/{ch["id"]}', json={
        'title': ch['title'], 'body': '新的编辑内容。', 'questions': [], 'approved': False, 'expected_revision': ch['edit_revision']})
    assert response.status_code == 200
    history = client.get(f'/api/projects/{pid}/editorial/chapters/{ch["id"]}/history').json()
    assert history[0]['snapshot']['body'] == original
    response = client.post(f'/api/projects/{pid}/editorial/chapters/{ch["id"]}/restore', json={
        'history_id': history[0]['id'], 'expected_revision': ch['edit_revision'] + 1})
    assert response.status_code == 200
    restored = client.get(f'/api/projects/{pid}').json()['books'][0]['chapters'][0]
    assert restored['body'] == original and restored['edit_revision'] == ch['edit_revision'] + 2
    assert not restored['approved']


def test_cancelled_editorial_job_does_not_save_results(client):
    pid, _ = setup_project(client)
    jid = client.post(f'/api/projects/{pid}/editorial/plan').json()['job_id']
    with db.connect() as c:
        c.execute('UPDATE jobs SET cancel_requested=1 WHERE id=?', (jid,))
    with pytest.raises(JobCancelled):
        run(jid)
    assert client.get(f'/api/projects/{pid}/editorial').json()['plans'] == []


def test_editorial_routes_do_not_expose_other_accounts(client):
    pid, _ = setup_project(client)
    p = plan(client, pid)
    client.post('/api/logout')
    client.post('/api/register', json={'email': 'stranger@example.com', 'password': 'long-test-password', 'name': '陌生人'})
    assert client.get(f'/api/projects/{pid}/editorial').status_code == 404
    assert client.post(f'/api/projects/{pid}/editorial/plan').status_code == 404
    assert client.post(f'/api/projects/{pid}/editorial/plans/{p["id"]}/draft').status_code == 404


def test_live_draft_packs_multiple_sources_and_keeps_all_text(monkeypatch):
    monkeypatch.setattr(config, 'AI_MODE', 'live')
    received = []
    def model(system, payload):
        received.extend(payload['materials'])
        return {'body': '|'.join(part['text'] for part in payload['materials']), 'questions': []}
    monkeypatch.setattr(editorial, 'structured', model)
    sources = [{'id': 'a', 'text': '甲' * 7001}, {'id': 'b', 'text': '乙' * 5001}]
    result = editorial.draft_book({'project': {'name': '讲述者', 'style': '质朴'}, 'sources': sources, 'facts': [],
        'chapters': [{'title': '成长', 'summary': '童年到求学', 'source_ids': ['a', 'b'], 'questions': []}]}, lambda _: None)
    assert ''.join(part['text'] for part in received if part['source_id'] == 'a') == sources[0]['text']
    assert ''.join(part['text'] for part in received if part['source_id'] == 'b') == sources[1]['text']
    assert result[0]['source_ids'] == ['a', 'b']
    assert len(received) == 3


def test_check_refuses_to_apply_results_after_concurrent_edit(client):
    pid, _ = setup_project(client, ('我于1950年开始上学。', '我于1951年开始上学。'))
    p = plan(client, pid)
    approve_plan(client, pid, p)
    book = draft(client, pid, p)
    jid = client.post(f'/api/projects/{pid}/editorial/books/{book["id"]}/check').json()['job_id']
    # Simulate an independent change bypassing the endpoint's idle lock.
    with db.connect() as c:
        c.execute('UPDATE chapters SET body=? WHERE id=?', ('编辑的新版本。', book['chapters'][0]['id']))
    with pytest.raises(ai.AIError, match='书稿已被修改'):
        run(jid)
    assert client.get(f'/api/projects/{pid}/editorial').json()['checks'] == []
