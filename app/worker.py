"""Two task lanes and one renewable single-host consumer lease."""
import asyncio
import json
import logging
import os
import threading
from . import ai, config, runtime
from .db import connect, now, uid, audit


def enqueue(c, project, kind, payload):
    runtime.enforce_job(c, project, kind)
    jid = uid()
    c.execute('INSERT INTO jobs(id,project_id,kind,payload,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
              (jid, project, kind, json.dumps(payload, ensure_ascii=False), 'queued', now(), now()))
    return jid


def process_one(kinds=None, stop_event=None):
    """Claim one eligible job atomically; no filter preserves the manual API."""
    if stop_event is not None and stop_event.is_set():
        return False
    eligible = tuple(kinds) if kinds is not None else None
    if eligible == ():
        return False
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        query = "SELECT j.* FROM jobs j LEFT JOIN project_runtime r ON r.project_id=j.project_id WHERE j.status='queued' AND j.cancel_requested=0 AND COALESCE(r.enabled,1)=1"
        if eligible is not None:
            query += ' AND j.kind IN (' + ','.join('?' for _ in eligible) + ')'
        row = c.execute(query + ' ORDER BY j.created_at LIMIT 1', eligible or ()).fetchone()
        if not row:
            return False
        job = dict(row)
        if job['attempts'] >= int(os.getenv('MAX_JOB_ATTEMPTS', '5')):
            c.execute("UPDATE jobs SET status='failed',error='任务已达到部署方设置的重试次数上限。',updated_at=? WHERE id=?", (now(), job['id']))
            return True
        c.execute("UPDATE jobs SET status='running',attempts=attempts+1,updated_at=? WHERE id=?", (now(), job['id']))

    def check():
        if stop_event is not None and stop_event.is_set():
            raise ai.AIError('服务停止中断了任务，请重试。')
        with connect() as c:
            runtime.assert_job_active(c, job['id'])

    def progress(message):
        check()
        with connect() as c:
            c.execute('UPDATE jobs SET progress=?,updated_at=? WHERE id=?', (message, now(), job['id']))

    runtime.set_current_job(job['id'])
    try:
        check()
        payload = json.loads(job['payload'])
        with connect() as c:
            project = dict(c.execute('SELECT * FROM projects WHERE id=?', (job['project_id'],)).fetchone())
            brief = c.execute('SELECT content FROM briefs WHERE project_id=?', (job['project_id'],)).fetchone()
            project['_brief'] = json.loads(brief['content']) if brief else {}
        if job['kind'] == 'transcribe':
            with connect() as c:
                source = dict(c.execute('SELECT * FROM sources WHERE id=?', (payload['source_id'],)).fetchone())
            text, segments = ai.transcribe(config.UPLOADS / source['path'], progress,
                                          checkpoint_source_id=source['id'], check_cancel=check)
            check()
            with connect() as c:
                c.execute('BEGIN IMMEDIATE')
                runtime.assert_job_active(c, job['id'])
                c.execute('UPDATE sources SET text=?,segments=?,confirmed=0 WHERE id=?', (text, json.dumps(segments, ensure_ascii=False), source['id']))
                c.execute('UPDATE projects SET revision=revision+1 WHERE id=?', (project['id'],))
                audit(c, project['id'], 'system', '音频转写完成，等待人工核对')
                runtime.finish_job(c, job['id'])
        elif job['kind'] == 'interview':
            answer = ai.followup(project, payload['messages'])
            check()
            with connect() as c:
                c.execute('BEGIN IMMEDIATE')
                runtime.assert_job_active(c, job['id'])
                c.execute('INSERT INTO messages(project_id,role,content,created_at) VALUES(?,?,?,?)', (project['id'], 'assistant', answer, now()))
                runtime.finish_job(c, job['id'])
        elif job['kind'] == 'generate':
            drafts = []
            for index, source in enumerate(payload['sources']):
                parts = [source['text'][i:i + 10000] for i in range(0, len(source['text']), 10000)]
                for part_index, part in enumerate(parts):
                    progress(f'正在撰写素材 {index + 1}/{len(payload["sources"])}，片段 {part_index + 1}/{len(parts)}')
                    drafts.append(ai.write_chapter(project, {**source, 'text': part}))
            check()
            with connect() as c:
                c.execute('BEGIN IMMEDIATE')
                runtime.assert_job_active(c, job['id'])
                bid = uid()
                c.execute('INSERT INTO books VALUES(?,?,?,?,?)', (bid, project['id'], payload['revision'], now(), config.AI_MODE))
                for index, draft in enumerate(drafts):
                    c.execute('INSERT INTO chapters(id,book_id,position,title,body,source_ids,questions,approved) VALUES(?,?,?,?,?,?,?,0)', (uid(), bid, index, draft['title'], draft['body'], json.dumps(draft['source_ids']), json.dumps(draft['questions'], ensure_ascii=False)))
                audit(c, project['id'], 'system', '生成新稿件版本，保留历史版本')
                runtime.finish_job(c, job['id'])
        elif job['kind'] in ('editorial_plan', 'editorial_draft', 'editorial_check'):
            from .editorial import run_job
            run_job(job, payload, progress)
            # Editorial handlers commit result and finish_job in one transaction.
        else:
            raise ai.AIError('未知任务类型')
    except runtime.JobCancelled:
        with connect() as c:
            c.execute("UPDATE jobs SET status='cancelled',progress='已取消；本次结果未写入',error='',updated_at=? WHERE id=? AND status!='done'", (now(), job['id']))
    except Exception as e:
        logging.exception('Job %s failed (%s)', job['id'], type(e).__name__)
        error = str(e) if isinstance(e, ai.AIError) else '任务处理失败，请查看服务端日志或重试。'
        with connect() as c:
            row = c.execute('SELECT cancel_requested FROM jobs WHERE id=?', (job['id'],)).fetchone()
            if row and row['cancel_requested']:
                c.execute("UPDATE jobs SET status='cancelled',progress='已取消',error='',updated_at=? WHERE id=?", (now(), job['id']))
            else:
                c.execute("UPDATE jobs SET status='failed',error=?,updated_at=? WHERE id=?", (error, now(), job['id']))
    finally:
        runtime.set_current_job(None)
    return True


async def _consume(kinds, stop_event):
    while not stop_event.is_set():
        pending = asyncio.create_task(asyncio.to_thread(process_one, kinds, stop_event))
        try:
            processed = await asyncio.shield(pending)
        except asyncio.CancelledError:
            stop_event.set()
            await pending
            raise
        if not processed:
            await asyncio.sleep(1)


async def _heartbeat(token, stop_event):
    while not stop_event.is_set():
        await asyncio.sleep(10)
        await asyncio.to_thread(runtime.renew_lease, token)


async def run():
    # The lease prevents a second process from resetting a live consumer's jobs.
    token = uid()
    runtime.acquire_lease(token)
    stop_event = threading.Event()
    consumers = []
    group = None
    try:
        with connect() as c:
            c.execute("UPDATE jobs SET status=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'failed' END,error='服务重启中断了任务，请重试。',updated_at=? WHERE status='running'", (now(),))
        consumers = [
            asyncio.create_task(_consume(('interview',), stop_event), name='studio-interview-queue'),
            asyncio.create_task(_consume(('transcribe', 'generate', 'editorial_plan', 'editorial_draft', 'editorial_check'), stop_event), name='studio-batch-queue'),
            asyncio.create_task(_heartbeat(token, stop_event), name='studio-lease-heartbeat'),
        ]
        group = asyncio.gather(*consumers)
        await asyncio.shield(group)
    finally:
        stop_event.set()
        for consumer in consumers:
            if not consumer.done():
                consumer.cancel()
        await asyncio.gather(*consumers, return_exceptions=True)
        if group is not None and group.done() and not group.cancelled():
            group.exception()
        runtime.release_lease(token)


def main():
    from .db import init
    init()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except runtime.WorkerAlreadyRunning as e:
        raise SystemExit(str(e))


if __name__ == '__main__':
    main()
