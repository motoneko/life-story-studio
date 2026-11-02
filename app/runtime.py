"""Single-host task controls, explicit budgets and private ASR checkpoints."""
import os
import time
import threading
from decimal import Decimal
from fastapi import HTTPException, Depends
from pydantic import BaseModel, Field
from .db import connect, now, uid, audit


class JobCancelled(RuntimeError):
    pass


class WorkerAlreadyRunning(RuntimeError):
    pass


def init_schema(c):
    # Individual statements do not implicitly commit a caller's transaction.
    c.execute('''CREATE TABLE IF NOT EXISTS project_runtime(
        project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
        enabled INTEGER NOT NULL DEFAULT 1,
        max_jobs INTEGER NOT NULL DEFAULT 500,
        max_audio_mb INTEGER NOT NULL DEFAULT 2048)''')
    c.execute('''CREATE TABLE IF NOT EXISTS usage_entries(
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        category TEXT NOT NULL, minutes INTEGER NOT NULL, amount_minor INTEGER NOT NULL,
        currency TEXT NOT NULL, note TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS asr_checkpoints(
        source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
        fingerprint TEXT NOT NULL, chunk_index INTEGER NOT NULL,
        result TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(source_id,fingerprint,chunk_index))''')
    c.execute('''CREATE TABLE IF NOT EXISTS worker_lease(
        name TEXT PRIMARY KEY, token TEXT NOT NULL, expires REAL NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS provider_usage(
        id TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,model TEXT NOT NULL,input_tokens INTEGER,output_tokens INTEGER,
        audio_seconds REAL,created_at TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS project_rates(
        project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
        input_minor_per_million TEXT NOT NULL,output_minor_per_million TEXT NOT NULL,
        audio_minor_per_minute TEXT NOT NULL,currency TEXT NOT NULL)''')
    columns = {row['name'] for row in c.execute('PRAGMA table_info(jobs)')}
    for column, definition in (('cancel_requested', 'INTEGER NOT NULL DEFAULT 0'), ('attempts', 'INTEGER NOT NULL DEFAULT 0')):
        if column not in columns:
            c.execute(f'ALTER TABLE jobs ADD COLUMN {column} {definition}')


def ensure_project(c, pid):
    c.execute('INSERT OR IGNORE INTO project_runtime(project_id,max_jobs,max_audio_mb) VALUES(?,?,?)',
              (pid, int(os.getenv('MAX_PROJECT_JOBS', '500')), int(os.getenv('MAX_PROJECT_AUDIO_MB', '2048'))))
    return dict(c.execute('SELECT * FROM project_runtime WHERE project_id=?', (pid,)).fetchone())


def enforce_job(c, pid, kind):
    limits = ensure_project(c, pid)
    if not limits['enabled']:
        raise HTTPException(409, '本项目已暂停 AI 处理，请由创建者恢复后再提交任务')
    count = c.execute('SELECT count(*) FROM jobs WHERE project_id=?', (pid,)).fetchone()[0]
    if count >= limits['max_jobs']:
        raise HTTPException(409, '本项目任务额度已用完，请在用量页面检查或调整允许范围内的额度')


def check_retry(c, pid, jid):
    limits = ensure_project(c, pid)
    if not limits['enabled']:
        raise HTTPException(409, '本项目已暂停 AI 处理，恢复后才能重试')
    job = c.execute('SELECT attempts FROM jobs WHERE id=? AND project_id=?', (jid, pid)).fetchone()
    if not job:
        raise HTTPException(404, '任务不存在')
    if job['attempts'] >= int(os.getenv('MAX_JOB_ATTEMPTS', '5')):
        raise HTTPException(409, '任务已达到部署方设置的重试次数上限')


def audio_bytes(c, pid):
    from . import config
    total = 0
    for row in c.execute('SELECT path FROM sources WHERE project_id=? AND path IS NOT NULL', (pid,)):
        path = config.UPLOADS / row['path']
        try:
            total += path.stat().st_size
        except FileNotFoundError:
            pass
    return total


def check_upload(c, pid, size_bytes):
    limits = ensure_project(c, pid)
    if not limits['enabled']:
        raise HTTPException(409, '本项目已暂停 AI 处理；恢复后才能提交录音识别')
    if size_bytes + audio_bytes(c, pid) > limits['max_audio_mb'] * 1024 * 1024:
        raise HTTPException(413, '项目音频存储额度不足，请删除不再保留的录音或调整额度')


def assert_job_active(c, jid):
    row = c.execute('''SELECT j.status,j.cancel_requested,COALESCE(r.enabled,1) AS enabled
        FROM jobs j LEFT JOIN project_runtime r ON r.project_id=j.project_id WHERE j.id=?''', (jid,)).fetchone()
    if not row or row['status'] != 'running' or row['cancel_requested'] or not row['enabled']:
        raise JobCancelled('任务已取消或项目已暂停，未保存本次处理结果')


def finish_job(c, jid):
    assert_job_active(c, jid)
    c.execute("UPDATE jobs SET status='done',progress='已完成',updated_at=? WHERE id=?", (now(), jid))


def acquire_lease(token, seconds=60):
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute("SELECT token,expires FROM worker_lease WHERE name='consumer'").fetchone()
        if row and row['expires'] > time.time() and row['token'] != token:
            raise WorkerAlreadyRunning('已有任务进程正在运行；Web 进程请设 RUN_WORKER=false，只运行一个独立 worker。')
        c.execute("INSERT INTO worker_lease(name,token,expires) VALUES('consumer',?,?) ON CONFLICT(name) DO UPDATE SET token=excluded.token,expires=excluded.expires", (token, time.time() + seconds))


def renew_lease(token, seconds=60):
    with connect() as c:
        changed = c.execute("UPDATE worker_lease SET expires=? WHERE name='consumer' AND token=?", (time.time() + seconds, token)).rowcount
        if changed != 1:
            raise WorkerAlreadyRunning('任务进程失去租约，停止处理。')


def release_lease(token):
    with connect() as c:
        c.execute("DELETE FROM worker_lease WHERE name='consumer' AND token=?", (token,))


_call_context = threading.local()


def set_current_job(jid):
    _call_context.job_id = jid


def record_provider_usage(kind, model, response, audio_seconds=None):
    """Observed response usage only, not inferred or invented token consumption."""
    jid = getattr(_call_context, 'job_id', None)
    if not jid:
        return
    usage = response.get('usage', {}) if isinstance(response, dict) else {}
    usage = usage if isinstance(usage, dict) else {}
    def token(*keys):
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return None
    with connect() as c:
        if not c.execute('SELECT 1 FROM jobs WHERE id=?', (jid,)).fetchone():
            return
        c.execute('INSERT INTO provider_usage VALUES(?,?,?,?,?,?,?,?)',
                  (uid(), jid, kind, model, token('prompt_tokens', 'input_tokens'), token('completion_tokens', 'output_tokens'), audio_seconds, now()))


class RatesInput(BaseModel):
    input_minor_per_million: Decimal = Field(ge=0, le=1000000000)
    output_minor_per_million: Decimal = Field(ge=0, le=1000000000)
    audio_minor_per_minute: Decimal = Field(ge=0, le=1000000000)
    currency: str = Field(default='CNY', pattern=r'^[A-Z]{3}$')


class ProcessingInput(BaseModel):
    enabled: bool


class LimitsInput(BaseModel):
    max_jobs: int = Field(ge=1, le=100000)
    max_audio_mb: int = Field(ge=1, le=1048576)


class CostInput(BaseModel):
    category: str = Field(min_length=1, max_length=40)
    minutes: int = Field(default=0, ge=0, le=1000000)
    amount_minor: int = Field(ge=0, le=100000000000)
    currency: str = Field(default='CNY', pattern=r'^[A-Z]{3}$')
    note: str = Field(default='', max_length=500)


def register(app, user, project_access, idle):
    @app.post('/api/projects/{pid}/jobs/{jid}/cancel')
    def cancel_job(pid: str, jid: str, current=Depends(user)):
        with connect() as c:
            c.execute('BEGIN IMMEDIATE')
            project_access(c, pid, current)
            row = c.execute('SELECT status FROM jobs WHERE id=? AND project_id=?', (jid, pid)).fetchone()
            if not row:
                raise HTTPException(404, '任务不存在')
            if row['status'] in ('queued', 'running'):
                status = 'cancelled' if row['status'] == 'queued' else 'running'
                c.execute('UPDATE jobs SET cancel_requested=1,status=?,progress=?,updated_at=? WHERE id=?',
                          (status, '已取消' if status == 'cancelled' else '取消中：当前外部调用结束后停止，已发生费用不会撤回', now(), jid))
                audit(c, pid, current['email'], '请求取消任务')
            else:
                status = row['status']
        return {'status': status}

    @app.put('/api/projects/{pid}/processing')
    def processing(pid: str, data: ProcessingInput, current=Depends(user)):
        with connect() as c:
            c.execute('BEGIN IMMEDIATE')
            project_access(c, pid, current, owner=True)
            ensure_project(c, pid)
            c.execute('UPDATE project_runtime SET enabled=? WHERE project_id=?', (data.enabled, pid))
            if not data.enabled:
                c.execute("UPDATE jobs SET status='cancelled',cancel_requested=1,progress='项目已暂停',updated_at=? WHERE project_id=? AND status='queued'", (now(), pid))
                c.execute("UPDATE jobs SET cancel_requested=1,progress='项目已暂停，等待当前外部调用结束',updated_at=? WHERE project_id=? AND status='running'", (now(), pid))
            audit(c, pid, current['email'], '恢复 AI 处理（不会自动重试取消任务）' if data.enabled else '暂停 AI 处理并取消待处理任务')
        return {'enabled': data.enabled}

    @app.get('/api/projects/{pid}/usage')
    def usage(pid: str, current=Depends(user)):
        with connect() as c:
            project_access(c, pid, current)
            limits = ensure_project(c, pid)
            counts = [dict(r) for r in c.execute('SELECT kind,status,count(*) AS count,sum(attempts) AS attempts FROM jobs WHERE project_id=? GROUP BY kind,status', (pid,))]
            entries = [dict(r) for r in c.execute('SELECT * FROM usage_entries WHERE project_id=? ORDER BY created_at DESC', (pid,))]
            totals = [dict(r) for r in c.execute('SELECT currency,sum(amount_minor) AS amount_minor,sum(minutes) AS minutes FROM usage_entries WHERE project_id=? GROUP BY currency', (pid,))]
            checkpoints = c.execute('SELECT count(*) FROM asr_checkpoints a JOIN sources s ON s.id=a.source_id WHERE s.project_id=?', (pid,)).fetchone()[0]
            size = audio_bytes(c, pid)
            observed = [dict(r) for r in c.execute('''SELECT u.kind,u.model,count(*) AS successful_responses,
                sum(u.input_tokens) AS input_tokens,sum(u.output_tokens) AS output_tokens,
                sum(u.audio_seconds) AS audio_seconds,
                sum(CASE WHEN u.input_tokens IS NULL AND u.kind='text' THEN 1 ELSE 0 END) AS responses_without_token_usage
                FROM provider_usage u JOIN jobs j ON j.id=u.job_id WHERE j.project_id=? GROUP BY u.kind,u.model''', (pid,))]
            rates_row = c.execute('SELECT * FROM project_rates WHERE project_id=?', (pid,)).fetchone()
            rates = dict(rates_row) if rates_row else None
            estimate = None
            if rates:
                amount = Decimal(0)
                for item in observed:
                    if item['kind'] == 'text':
                        amount += Decimal(item['input_tokens'] or 0) * Decimal(rates['input_minor_per_million']) / 1000000
                        amount += Decimal(item['output_tokens'] or 0) * Decimal(rates['output_minor_per_million']) / 1000000
                    elif item['kind'] == 'asr':
                        amount += Decimal(str(item['audio_seconds'] or 0)) * Decimal(rates['audio_minor_per_minute']) / 60
                estimate = {'amount_minor': str(amount.quantize(Decimal('0.01'))), 'currency': rates['currency'],
                            'note': '按当前统一单价估算已返回 usage 的 token 与成功 ASR 分片时长；未返回用量、失败调用、供应商其他计费均未计入。更换模型应重新设价并核对实际账单。'}
        return {'enabled': bool(limits['enabled']), 'limits': limits, 'deployment_limits': {'max_jobs': int(os.getenv('MAX_PROJECT_JOBS', '500')), 'max_audio_mb': int(os.getenv('MAX_PROJECT_AUDIO_MB', '2048'))}, 'job_counts': counts, 'audio_bytes': size, 'completed_asr_chunks': checkpoints,
                'cost_entries': entries, 'cost_totals': totals, 'provider_usage_summary': observed, 'rates': rates, 'estimate': estimate, 'note': '金额为人工登记实际费用，单位为货币最小单位（CNY 为分）。任务次数和已缓存分片不是供应商 token 账单。'}

    @app.put('/api/projects/{pid}/limits')
    def set_limits(pid: str, data: LimitsInput, current=Depends(user)):
        if data.max_jobs > int(os.getenv('MAX_PROJECT_JOBS', '500')) or data.max_audio_mb > int(os.getenv('MAX_PROJECT_AUDIO_MB', '2048')):
            raise HTTPException(422, '不得超过部署方设置的项目额度上限')
        with connect() as c:
            project_access(c, pid, current, owner=True)
            ensure_project(c, pid)
            c.execute('UPDATE project_runtime SET max_jobs=?,max_audio_mb=? WHERE project_id=?', (data.max_jobs, data.max_audio_mb, pid))
            audit(c, pid, current['email'], '调整项目任务与音频额度')
        return {'ok': True}

    @app.post('/api/projects/{pid}/costs')
    def add_cost(pid: str, data: CostInput, current=Depends(user)):
        with connect() as c:
            project_access(c, pid, current, owner=True)
            eid = uid()
            c.execute('INSERT INTO usage_entries VALUES(?,?,?,?,?,?,?,?,?)', (eid, pid, data.category, data.minutes, data.amount_minor, data.currency, data.note, current['email'], now()))
            audit(c, pid, current['email'], '登记项目人工/外部服务实际成本')
        return {'id': eid}

    @app.put('/api/projects/{pid}/rates')
    def set_rates(pid: str, data: RatesInput, current=Depends(user)):
        with connect() as c:
            project_access(c, pid, current, owner=True)
            c.execute('INSERT INTO project_rates VALUES(?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET input_minor_per_million=excluded.input_minor_per_million,output_minor_per_million=excluded.output_minor_per_million,audio_minor_per_minute=excluded.audio_minor_per_minute,currency=excluded.currency',
                      (pid, str(data.input_minor_per_million), str(data.output_minor_per_million), str(data.audio_minor_per_minute), data.currency))
            audit(c, pid, current['email'], '更新模型成本估算单价')
        return {'ok': True}
