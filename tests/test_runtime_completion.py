import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from app import ai, config, db, runtime, worker


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB', tmp_path / 'runtime.db')
    monkeypatch.setattr(config, 'UPLOADS', tmp_path / 'uploads')
    config.UPLOADS.mkdir()
    monkeypatch.setattr(config, 'AI_MODE', 'demo')
    db.init()
    with db.connect() as c:
        runtime.init_schema(c)
        c.execute('INSERT INTO users VALUES(?,?,?,?)', ('owner', 'runtime@example.com', 'unused', '用户'))
        c.execute('INSERT INTO users VALUES(?,?,?,?)', ('stranger', 'stranger@example.com', 'unused', '其他人'))
        for pid, owner in [('p', 'owner'), ('other', 'stranger')]:
            c.execute('INSERT INTO projects(id,owner_id,name,title,style,consent,created_at) VALUES(?,?,?,?,?,?,?)', (pid, owner, '讲述者', '自传', '质朴亲切', '{}', db.now()))
    app = FastAPI()
    def current():
        return {'id': 'owner', 'email': 'runtime@example.com'}
    def access(c, pid, current, owner=False):
        p = c.execute('SELECT * FROM projects WHERE id=? AND owner_id=?', (pid, current['id'])).fetchone()
        if not p:
            raise HTTPException(404)
        return dict(p)
    runtime.register(app, current, access, lambda c, pid: None)
    with TestClient(app) as client:
        yield client


def get_job(jid):
    with db.connect() as c:
        return dict(c.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone())


def enqueue(kind='interview', payload=None, pid='p'):
    with db.connect() as c:
        return worker.enqueue(c, pid, kind, payload or {'messages': []})


def test_cancel_running_does_not_store_provider_result(environment, monkeypatch):
    started, release = threading.Event(), threading.Event()
    def answer(*args):
        started.set()
        assert release.wait(5)
        return '不应在取消后保存的追问'
    monkeypatch.setattr(ai, 'followup', answer)
    jid = enqueue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(worker.process_one)
        try:
            assert started.wait(2)
            result = environment.post(f'/api/projects/p/jobs/{jid}/cancel')
            assert result.status_code == 200
            assert result.json()['status'] == 'running'  # Holds project mutation lock until the call returns.
        finally:
            release.set()
        running.result(3)
    assert get_job(jid)['status'] == 'cancelled'
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM messages WHERE project_id=?', ('p',)).fetchone()[0] == 0


def test_pause_cancels_waiting_jobs_and_blocks_new_processing(environment):
    jid = enqueue()
    assert environment.put('/api/projects/p/processing', json={'enabled': False}).status_code == 200
    assert get_job(jid)['status'] == 'cancelled'
    with pytest.raises(HTTPException) as error:
        enqueue()
    assert error.value.status_code == 409
    assert worker.process_one() is False
    assert environment.put('/api/projects/p/processing', json={'enabled': True}).status_code == 200
    assert worker.process_one() is False  # Resume never retries automatically.
    assert environment.get('/api/projects/p/usage').json()['enabled'] is True


def test_limits_upload_costs_and_tenant_isolation(environment):
    assert environment.put('/api/projects/p/limits', json={'max_jobs': 1, 'max_audio_mb': 1}).status_code == 200
    enqueue()
    with pytest.raises(HTTPException):
        enqueue()
    with db.connect() as c:
        with pytest.raises(HTTPException) as error:
            runtime.check_upload(c, 'p', 1024*1024+1)
        assert error.value.status_code == 413
    cost = {'category': '人工听校', 'minutes': 40, 'amount_minor': 8000, 'currency': 'CNY', 'note': '实际人工费用'}
    assert environment.post('/api/projects/p/costs', json=cost).status_code == 200
    usage = environment.get('/api/projects/p/usage').json()
    assert usage['cost_totals'] == [{'currency': 'CNY', 'amount_minor': 8000, 'minutes': 40}]
    assert '不是供应商 token 账单' in usage['note']
    other = enqueue(pid='other')
    assert environment.post(f'/api/projects/other/jobs/{other}/cancel').status_code == 404
    assert get_job(other)['status'] == 'queued'
    assert environment.get('/api/projects/other/usage').status_code == 404


def test_worker_lease_prevents_duplicate_recovery(environment):
    jid = enqueue()
    with db.connect() as c:
        c.execute("UPDATE jobs SET status='running' WHERE id=?", (jid,))
    runtime.acquire_lease('first')
    with pytest.raises(runtime.WorkerAlreadyRunning):
        runtime.acquire_lease('second')
    assert get_job(jid)['status'] == 'running'
    runtime.release_lease('second')
    with pytest.raises(runtime.WorkerAlreadyRunning):
        runtime.acquire_lease('second')
    runtime.release_lease('first')
    runtime.acquire_lease('second')
    runtime.renew_lease('second')
    runtime.release_lease('second')


def test_job_retry_budget_blocks_external_calls(environment, monkeypatch):
    jid = enqueue()
    with db.connect() as c:
        c.execute('UPDATE jobs SET attempts=5 WHERE id=?', (jid,))
    monkeypatch.setattr(ai, 'followup', lambda *a: pytest.fail('provider must not be called'))
    assert worker.process_one()
    assert get_job(jid)['status'] == 'failed'
    assert '重试次数上限' in get_job(jid)['error']


def test_asr_checkpoints_skip_successes_and_bind_audio_and_model(environment, monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'AI_MODE', 'live')
    monkeypatch.setenv('ASR_API_KEY', 'private-never-stored')
    monkeypatch.setenv('ASR_MODEL', 'model-a')
    monkeypatch.setenv('ASR_BASE_URL', 'https://asr.test/v1')
    monkeypatch.setattr(ai.shutil, 'which', lambda _: '/fake/ffmpeg')
    audio = tmp_path / 'source.wav'
    audio.write_bytes(b'RIFF-source-a')
    with db.connect() as c:
        c.execute('INSERT INTO sources(id,project_id,kind,name,path,created_at) VALUES(?,?,?,?,?,?)', ('s', 'p', 'audio', '采访', 'source.wav', db.now()))
    def run(args, **kw):
        if args[0] == 'ffprobe':
            return subprocess.CompletedProcess(args, 0, stdout='{"format":{"duration":"720"}}')
        Path(args[-1].replace('%04d', '0000')).write_bytes(b'chunk-one')
        Path(args[-1].replace('%04d', '0001')).write_bytes(b'chunk-two')
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(ai.subprocess, 'run', run)
    original_client = httpx.Client
    calls = []
    fail_second = [True]
    def handle(request):
        second = b'chunk-two' in request.content
        calls.append(2 if second else 1)
        if second and fail_second[0]:
            fail_second[0] = False
            return httpx.Response(503)
        return httpx.Response(200, json={'text': '原话二' if second else '原话一', 'segments': [{'start': 0, 'end': 5, 'text': '原话二' if second else '原话一', 'speaker': 'speaker_0'}]})
    monkeypatch.setattr(ai.httpx, 'Client', lambda **kw: original_client(transport=httpx.MockTransport(handle), **kw))
    with pytest.raises(ai.AIError):
        ai.transcribe(audio, lambda _: None, checkpoint_source_id='s')
    assert calls == [1, 2]
    text, segments = ai.transcribe(audio, lambda _: None, checkpoint_source_id='s')
    assert calls == [1, 2, 2]
    assert text == '原话一\n\n原话二'
    assert segments[1]['start'] == 600 and segments[1]['end'] == 605
    assert segments[0]['precision'] == 'provider_segment'
    assert segments[0]['speaker_scope'] != segments[1]['speaker_scope']
    with db.connect() as c:
        cache = [dict(r) for r in c.execute('SELECT * FROM asr_checkpoints')]
    assert 'private-never-stored' not in json.dumps(cache)
    monkeypatch.setenv('ASR_MODEL', 'model-b')
    ai.transcribe(audio, lambda _: None, checkpoint_source_id='s')
    assert calls[-2:] == [1, 2] and len(calls) == 5
    audio.write_bytes(b'RIFF-new-source')
    ai.transcribe(audio, lambda _: None, checkpoint_source_id='s')
    assert calls[-2:] == [1, 2] and len(calls) == 7


def test_asr_plain_response_does_not_invent_precise_timing_or_speaker():
    piece = ai._asr_segments({'text': '只有文字。'}, 600, 720)
    assert piece['segments'] == [{'start': 600, 'end': 720, 'text': '只有文字。', 'precision': 'chunk'}]


def test_observed_usage_and_configured_cost_estimate(environment):
    jid = enqueue()
    runtime.set_current_job(jid)
    try:
        runtime.record_provider_usage('text', 'test-model', {'usage': {'prompt_tokens': 1000000, 'completion_tokens': 100000}})
        runtime.record_provider_usage('text', 'test-model', {})
        runtime.record_provider_usage('asr', 'test-asr', {'usage': {'input_tokens': 1234}}, audio_seconds=90)
    finally:
        runtime.set_current_job(None)
    result = environment.put('/api/projects/p/rates', json={'input_minor_per_million': 1000, 'output_minor_per_million': 2000, 'audio_minor_per_minute': 10, 'currency': 'CNY'})
    assert result.status_code == 200
    report = environment.get('/api/projects/p/usage').json()
    assert report['estimate']['amount_minor'] == '1215.00'
    text_usage = next(row for row in report['provider_usage_summary'] if row['kind'] == 'text')
    assert text_usage['responses_without_token_usage'] == 1
    assert text_usage['input_tokens'] == 1000000
    assert environment.put('/api/projects/other/rates', json={'input_minor_per_million': 1000, 'output_minor_per_million': 2000, 'audio_minor_per_minute': 10}).status_code == 404
