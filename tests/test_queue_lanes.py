"""An offline transcription must not occupy the online interview's consumer."""
import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import ai, config, db, worker


@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB', tmp_path / 'queue.sqlite3')
    monkeypatch.setattr(config, 'AI_MODE', 'demo')
    db.init()
    with db.connect() as c:
        c.execute('INSERT INTO users VALUES(?,?,?,?)', ('owner', 'queue@example.com', 'unused', '测试'))
        for pid in ('offline', 'online', 'interrupted'):
            c.execute('INSERT INTO projects(id,owner_id,name,title,style,consent,created_at) VALUES(?,?,?,?,?,?,?)',
                      (pid, 'owner', pid, '故事', '质朴亲切', '{}', db.now()))
    monkeypatch.setattr(ai, 'followup', lambda *args: '那是什么时候？')


def enqueue(pid, kind, payload):
    with db.connect() as c:
        return worker.enqueue(c, pid, kind, payload)


def status(jid):
    with db.connect() as c:
        return dict(c.execute('SELECT status,error FROM jobs WHERE id=?', (jid,)).fetchone())


def generation_payload(text='真实素材'):
    return {'revision': 0, 'sources': [{'id': 'source', 'name': '采访', 'text': text}]}


def test_interview_lane_passes_older_queued_batch(queue):
    batch = enqueue('offline', 'generate', generation_payload())
    online = enqueue('online', 'interview', {'messages': []})
    assert worker.process_one(('interview',)) is True
    assert status(online)['status'] == 'done'
    assert status(batch)['status'] == 'queued'
    assert worker.process_one(('interview',)) is False
    # The default call remains compatible and can claim any kind.
    assert worker.process_one() is True
    assert status(batch)['status'] == 'done'


def test_batch_lane_never_claims_interview(queue):
    online = enqueue('online', 'interview', {'messages': []})
    assert worker.process_one(('transcribe', 'generate')) is False
    assert status(online)['status'] == 'queued'
    assert worker.process_one(()) is False
    assert status(online)['status'] == 'queued'


def test_running_batch_does_not_block_other_project_interview(queue, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def slow_chapter(project, source):
        entered.set()
        assert release.wait(5), 'test did not release batch task'
        return {'title': '童年', 'body': '正文', 'source_ids': [source['id']], 'questions': []}
    monkeypatch.setattr(ai, 'write_chapter', slow_chapter)
    batch = enqueue('offline', 'generate', generation_payload())
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(worker.process_one, ('transcribe', 'generate'))
        try:
            assert entered.wait(2)
            online = enqueue('online', 'interview', {'messages': []})
            assert worker.process_one(('interview',)) is True
            assert status(online)['status'] == 'done'
            assert status(batch)['status'] == 'running'
        finally:
            release.set()
        assert running.result(timeout=2) is True
    assert status(batch)['status'] == 'done'


def test_atomic_claim_does_not_execute_one_interview_twice(queue, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def slow_interview(*args):
        calls.append('called')
        entered.set()
        assert release.wait(5)
        return '请继续讲。'
    monkeypatch.setattr(ai, 'followup', slow_interview)
    online = enqueue('online', 'interview', {'messages': []})
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(worker.process_one, ('interview',))
        try:
            assert entered.wait(2)
            assert worker.process_one(('interview',)) is False
        finally:
            release.set()
        assert running.result(timeout=2) is True
    assert calls == ['called']
    assert status(online)['status'] == 'done'


def test_run_recovers_once_and_joins_lanes_on_cancel(queue, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def slow_chapter(project, source):
        calls.append(source['text'])
        entered.set()
        assert release.wait(5)
        return {'title': '章节', 'body': '正文', 'source_ids': [source['id']], 'questions': []}
    monkeypatch.setattr(ai, 'write_chapter', slow_chapter)
    stale = enqueue('interrupted', 'interview', {'messages': []})
    with db.connect() as c:
        c.execute("UPDATE jobs SET status='running' WHERE id=?", (stale,))
    # Two parts: shutdown must not start the second external generation call.
    batch = enqueue('offline', 'generate', generation_payload('字' * 10001))
    online = enqueue('online', 'interview', {'messages': []})

    async def exercise():
        owner = asyncio.create_task(worker.run())
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            for _ in range(100):
                if status(online)['status'] == 'done':
                    break
                await asyncio.sleep(.01)
            assert status(online)['status'] == 'done'
            assert status(stale)['status'] == 'failed'
            # If another consumer reran startup recovery, this job would be failed.
            assert status(batch)['status'] == 'running'
            owner.cancel()
            await asyncio.sleep(.03)
            assert not owner.done(), 'shutdown returned while provider thread still ran'
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(owner, 2)
            assert not [t for t in asyncio.all_tasks() if t.get_name().startswith('studio-')]
        finally:
            release.set()
            if not owner.done():
                owner.cancel()
                await asyncio.gather(owner, return_exceptions=True)

    asyncio.run(exercise())
    assert len(calls) == 1
    assert status(batch)['status'] == 'failed'
    assert '服务停止' in status(batch)['error']
