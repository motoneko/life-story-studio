import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal
from fastapi import FastAPI, HTTPException, Request, Response, Depends, UploadFile, File, Form
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from . import config, worker, editorial, operations, runtime, accounts
from .db import connect, init, now, uid, audit
from .interview import QUESTIONS, SECTIONS, template
from .exports import docx_bytes, bundle
from .release import content_hash, current_release


@asynccontextmanager
async def lifespan(app):
    init()
    task = asyncio.create_task(worker.run()) if os.getenv('RUN_WORKER', 'true').lower() == 'true' else None
    if task:
        await asyncio.sleep(0)
        if task.done():
            await task
    yield
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title='拾光 · 口述自传工作室', lifespan=lifespan)
STATIC = Path(__file__).parent / 'static'
app.mount('/static', StaticFiles(directory=STATIC), name='static')


@app.middleware('http')
async def security(request, call_next):
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        origin = request.headers.get('origin')
        # Cross-origin cookies are not accepted; clients without Origin must send our custom header.
        if (origin and urlsplit(origin).netloc != request.headers.get('host')) or (not origin and request.headers.get('x-studio-request') != '1'):
            return Response('不允许跨站请求', status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    if request.url.path.startswith('/api'):
        response.headers['Cache-Control'] = 'no-store'
    return response


def user(request: Request):
    token = request.cookies.get('studio_session', '')
    with connect() as c:
        row = c.execute('SELECT users.id,users.email,users.name FROM users JOIN sessions ON users.id=sessions.user_id WHERE token=? AND expires>?', (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
    if not row:
        raise HTTPException(401, '请先登录')
    return dict(row)


def project_access(c, pid, current, owner=False):
    row = c.execute('SELECT * FROM projects WHERE id=?', (pid,)).fetchone()
    if not row or (row['owner_id'] != current['id'] and (owner or not c.execute('SELECT 1 FROM members WHERE project_id=? AND user_id=?', (pid, current['id'])).fetchone())):
        raise HTTPException(404, '项目不存在或无访问权限')
    project = dict(row)
    brief = c.execute('SELECT content FROM briefs WHERE project_id=?', (pid,)).fetchone()
    project['_brief'] = json.loads(brief['content']) if brief else {}
    return project


def idle(c, pid):
    if c.execute("SELECT 1 FROM jobs WHERE project_id=? AND status IN ('queued','running')", (pid,)).fetchone():
        raise HTTPException(409, '项目有任务正在处理，请稍后再操作')


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    result = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 310000).hex()
    return salt + ':' + result


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=180)
    password: str = Field(min_length=10, max_length=128)
    name: str = Field(default='家人', min_length=1, max_length=80)


attempts = {}


def throttle(request):
    key = request.client.host if request.client else 'unknown'
    clock = time.time()
    # Bound memory even for many clients; in production use a shared gateway limiter.
    if len(attempts) > 10000:
        attempts.clear()
    bucket = [t for t in attempts.get(key, []) if clock - t < 300]
    if len(bucket) >= 30:
        raise HTTPException(429, '尝试过于频繁，请 5 分钟后再试')
    attempts[key] = bucket + [clock]


def session(response, current):
    token = secrets.token_urlsafe(32)
    with connect() as c:
        c.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
        c.execute('INSERT INTO sessions VALUES(?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), current['id'], time.time() + 7 * 86400))
    response.set_cookie('studio_session', token, max_age=7 * 86400, httponly=True, secure=config.COOKIE_SECURE, samesite='strict')
    return {k: current[k] for k in ('id', 'name', 'email')}


@app.get('/')
def index():
    return FileResponse(STATIC / 'index.html')


@app.get('/api/config')
def settings():
    return {'mode': config.AI_MODE, 'max_upload_mb': config.MAX_UPLOAD // 1024 // 1024, 'questions': SECTIONS}


@app.post('/api/register')
def register(data: Credentials, response: Response, request: Request):
    throttle(request)
    email = data.email.strip().lower()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        raise HTTPException(422, '请输入有效邮箱')
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute('SELECT 1 FROM users WHERE email=?', (email,)).fetchone():
            raise HTTPException(409, '该邮箱已注册')
        current = {'id': uid(), 'email': email, 'name': data.name}
        c.execute('INSERT INTO users VALUES(?,?,?,?)', (current['id'], email, hash_password(data.password), data.name))
    return session(response, current)


@app.post('/api/login')
def login(data: Credentials, response: Response, request: Request):
    throttle(request)
    with connect() as c:
        row = c.execute('SELECT * FROM users WHERE email=?', (data.email.strip().lower(),)).fetchone()
    if not row:
        # Do comparable password work for unknown accounts.
        hash_password(data.password)
        raise HTTPException(401, '邮箱或密码不正确')
    if not hmac.compare_digest(hash_password(data.password, row['password'].split(':')[0]), row['password']):
        raise HTTPException(401, '邮箱或密码不正确')
    return session(response, dict(row))


@app.post('/api/logout')
def logout(request: Request, response: Response):
    with connect() as c:
        c.execute('DELETE FROM sessions WHERE token=?', (hashlib.sha256(request.cookies.get('studio_session', '').encode()).hexdigest(),))
    response.delete_cookie('studio_session')
    return {'ok': True}


@app.get('/api/me')
def me(current=Depends(user)):
    return current


class ProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=120)
    birth_year: str = Field(default='', max_length=20)
    style: Literal['质朴亲切', '温暖叙事', '纪实克制'] = '质朴亲切'
    consent: bool


@app.get('/api/projects')
def projects(current=Depends(user)):
    with connect() as c:
        rows = c.execute('SELECT p.*, (SELECT count(*) FROM sources s WHERE s.project_id=p.id) AS source_count,(SELECT count(*) FROM books b WHERE b.project_id=p.id) AS book_count FROM projects p WHERE p.owner_id=? OR p.id IN (SELECT project_id FROM members WHERE user_id=?) ORDER BY created_at DESC', (current['id'], current['id'])).fetchall()
    return [dict(r) for r in rows]


@app.post('/api/projects')
def create_project(data: ProjectInput, current=Depends(user)):
    if not data.consent:
        raise HTTPException(422, '创建前请取得讲述者或合法授权人的同意')
    pid = uid()
    with connect() as c:
        consent = json.dumps({'actor': current['email'], 'time': now(), 'version': '2026-11-v1', 'scope': '录音、AI转写、书稿生成及项目成员审阅；不含对外出版授权'}, ensure_ascii=False)
        c.execute('INSERT INTO projects(id,owner_id,name,title,birth_year,style,consent,created_at) VALUES(?,?,?,?,?,?,?,?)', (pid, current['id'], data.name, data.title, data.birth_year, data.style, consent, now()))
        c.execute('INSERT INTO messages(project_id,role,content,created_at) VALUES(?,?,?,?)', (pid, 'assistant', f'{data.name}，您好。我们慢慢聊，不着急。不想回答的问题可以跳过，也可以随时休息。' + QUESTIONS[0], now()))
        audit(c, pid, current['email'], '创建项目并确认处理授权')
    return {'id': pid}


@app.get('/api/projects/{pid}')
def detail(pid: str, current=Depends(user)):
    with connect() as c:
        p = project_access(c, pid, current)
        p['sources'] = [dict(r) for r in c.execute('SELECT id,kind,name,text,segments,confirmed,included,created_at,path IS NOT NULL AS has_audio FROM sources WHERE project_id=? ORDER BY created_at', (pid,))]
        p['messages'] = [dict(r) for r in c.execute('SELECT role,content,created_at FROM messages WHERE project_id=? ORDER BY id', (pid,))]
        p['jobs'] = [dict(r) for r in c.execute('SELECT id,kind,status,error,progress,created_at,cancel_requested FROM jobs WHERE project_id=? ORDER BY created_at DESC LIMIT 30', (pid,))]
        p['books'] = [dict(r) for r in c.execute('SELECT * FROM books WHERE project_id=? ORDER BY created_at DESC', (pid,))]
        for book in p['books']:
            book['chapters'] = [dict(r) for r in c.execute('SELECT * FROM chapters WHERE book_id=? ORDER BY position', (book['id'],))]
            book['content_sha256'] = content_hash(p, book, book['chapters'])
            book['release'] = current_release(c, p, book, book['chapters'])
        p['editorial'] = editorial.get_state(c, pid)
        p['operations'] = operations.get_state(c, pid)
        p['members'] = [dict(r) for r in c.execute('SELECT u.id,u.name,u.email FROM users u JOIN members m ON u.id=m.user_id WHERE m.project_id=?', (pid,))]
        p['audit'] = [dict(r) for r in c.execute('SELECT actor,action,created_at FROM audit WHERE project_id=? ORDER BY id DESC LIMIT 100', (pid,))]
    return p


@app.get('/api/projects/{pid}/template')
def download_template(pid: str, current=Depends(user)):
    with connect() as c:
        p = project_access(c, pid, current)
    return Response(template(p), media_type='text/markdown; charset=utf-8', headers={'Content-Disposition': 'attachment; filename="interview-template.md"'})


class TextInput(BaseModel):
    name: str = Field(default='采访笔记', min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=100000)


@app.post('/api/projects/{pid}/sources')
def add_source(pid: str, data: TextInput, current=Depends(user)):
    if not data.text.strip():
        raise HTTPException(422, '素材不能为空')
    sid = uid()
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        project_access(c, pid, current)
        idle(c, pid)
        c.execute('INSERT INTO sources(id,project_id,kind,name,text,created_at) VALUES(?,?,?,?,?,?)', (sid, pid, 'text', data.name, data.text, now()))
        c.execute('UPDATE projects SET revision=revision+1 WHERE id=?', (pid,))
        audit(c, pid, current['email'], '添加文字素材')
    return {'id': sid}


@app.post('/api/projects/{pid}/audio')
async def upload_audio(pid: str, file: UploadFile = File(...), recording_id: str | None = Form(default=None), current=Depends(user)):
    with connect() as c:
        project_access(c, pid, current)
        receipt = c.execute('SELECT source_id,job_id FROM upload_receipts WHERE project_id=? AND recording_id=?', (pid,recording_id)).fetchone() if recording_id else None
        if receipt:
            await file.close()
            return {'id': receipt['source_id'], 'job_id': receipt['job_id'], 'reused': True}
        idle(c, pid)
    if recording_id and not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', recording_id):
        raise HTTPException(422, '无效的录音标识')
    suffix = Path(file.filename or '').suffix.lower()
    if suffix not in {'.mp3', '.wav', '.m4a', '.webm', '.ogg', '.mp4', '.flac'}:
        raise HTTPException(422, '请选择 MP3、WAV、M4A、WEBM、OGG、MP4 或 FLAC 音频')
    sid = uid()
    path = config.UPLOADS / (sid + suffix)
    size = 0
    try:
        with path.open('wb') as target:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > config.MAX_UPLOAD:
                    raise HTTPException(413, f'文件不能超过 {config.MAX_UPLOAD // 1024 // 1024} MB')
                target.write(chunk)
        if size == 0:
            raise HTTPException(422, '文件为空')
        with connect() as c:
            c.execute('BEGIN IMMEDIATE')
            project_access(c, pid, current)
            receipt = c.execute('SELECT source_id,job_id FROM upload_receipts WHERE project_id=? AND recording_id=?', (pid,recording_id)).fetchone() if recording_id else None
            if receipt:
                path.unlink(missing_ok=True)
                return {'id':receipt['source_id'],'job_id':receipt['job_id'],'reused':True}
            idle(c, pid)
            runtime.check_upload(c, pid, size)
            c.execute('INSERT INTO sources(id,project_id,kind,name,path,created_at) VALUES(?,?,?,?,?,?)', (sid, pid, 'audio', (file.filename or '录音')[:120], path.name, now()))
            jid = worker.enqueue(c, pid, 'transcribe', {'source_id': sid})
            if recording_id:
                c.execute('INSERT INTO upload_receipts VALUES(?,?,?,?)', (pid,recording_id,sid,jid))
            c.execute('UPDATE projects SET revision=revision+1 WHERE id=?', (pid,))
            audit(c, pid, current['email'], '上传音频并提交转写')
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    return {'id': sid, 'job_id': jid}


class SourceUpdate(TextInput):
    text: str = Field(default='', max_length=100000)
    confirmed: bool = False
    included: bool | None = None


@app.put('/api/projects/{pid}/sources/{sid}')
def update_source(pid: str, sid: str, data: SourceUpdate, current=Depends(user)):
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        project_access(c, pid, current)
        idle(c, pid)
        existing = c.execute('SELECT * FROM sources WHERE id=? AND project_id=?', (sid, pid)).fetchone()
        if not existing:
            raise HTTPException(404, '素材不存在')
        included = bool(existing['included']) if data.included is None else data.included
        if (included or data.confirmed) and not data.text.strip():
            raise HTTPException(422, '入书或确认的逐字稿不能为空')
        c.execute('UPDATE sources SET name=?,text=?,confirmed=?,included=? WHERE id=?', (data.name, data.text, data.confirmed, included, sid))
        if existing['text'] != data.text or existing['name'] != data.name or existing['confirmed'] != data.confirmed or existing['included'] != included:
            c.execute('UPDATE projects SET revision=revision+1 WHERE id=?', (pid,))
        audit(c, pid, current['email'], '素材不入书' if not included else ('确认逐字稿' if data.confirmed else '修改逐字稿'))
    return {'ok': True}


@app.get('/api/projects/{pid}/sources/{sid}/audio')
def audio_file(pid: str, sid: str, current=Depends(user)):
    with connect() as c:
        project_access(c, pid, current)
        row = c.execute('SELECT path,name FROM sources WHERE id=? AND project_id=?', (sid, pid)).fetchone()
    if not row or not row['path'] or not (config.UPLOADS / row['path']).exists():
        raise HTTPException(404, '音频不存在')
    return FileResponse(config.UPLOADS / row['path'], filename=row['name'])


@app.delete('/api/projects/{pid}/sources/{sid}')
def delete_source(pid: str, sid: str, current=Depends(user)):
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        project_access(c, pid, current)
        idle(c, pid)
        row = c.execute('SELECT path FROM sources WHERE id=? AND project_id=?', (sid, pid)).fetchone()
        if not row:
            raise HTTPException(404, '素材不存在')
        c.execute('DELETE FROM sources WHERE id=?', (sid,))
        c.execute('UPDATE projects SET revision=revision+1 WHERE id=?', (pid,))
        audit(c, pid, current['email'], '删除素材；旧稿需重新核对')
    if row['path']:
        (config.UPLOADS / row['path']).unlink(missing_ok=True)
    return {'ok': True}


class MessageInput(BaseModel):
    text: str = Field(min_length=1, max_length=10000)
    source_id: str | None = None


@app.post('/api/projects/{pid}/messages')
def message(pid: str, data: MessageInput, current=Depends(user)):
    if not data.text.strip():
        raise HTTPException(422, '回答不能为空')
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        project_access(c, pid, current)
        idle(c, pid)
        audio_source = None
        if data.source_id:
            audio_source = c.execute("SELECT * FROM sources WHERE id=? AND project_id=? AND kind='audio'", (data.source_id, pid)).fetchone()
            if not audio_source:
                raise HTTPException(404, '回答的录音素材不存在')
        question = c.execute("SELECT content FROM messages WHERE project_id=? AND role='assistant' ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
        contextual_text = '采访者：' + (question['content'] if question else '') + '\n讲述者：' + data.text
        c.execute('INSERT INTO messages(project_id,role,content,created_at) VALUES(?,?,?,?)', (pid, 'user', data.text, now()))
        if data.text.strip() not in ('跳过', '休息', '暂停', '停止'):
            # One growing interview source produces a coherent chapter instead of one chapter per answer.
            existing = c.execute("SELECT id,text FROM sources WHERE project_id=? AND kind='online'", (pid,)).fetchone()
            if audio_source:
                # Reuse the audio's source rather than duplicate its answer in the online transcript.
                c.execute('UPDATE sources SET text=?,confirmed=0 WHERE id=?', (contextual_text, audio_source['id']))
            elif existing:
                c.execute('UPDATE sources SET text=?,confirmed=0 WHERE id=?', (existing['text'] + '\n\n' + contextual_text, existing['id']))
            else:
                c.execute('INSERT INTO sources(id,project_id,kind,name,text,created_at) VALUES(?,?,?,?,?,?)', (uid(), pid, 'online', '在线口述访谈', contextual_text, now()))
            c.execute('UPDATE projects SET revision=revision+1 WHERE id=?', (pid,))
        if data.text.strip() in ('休息', '暂停', '停止'):
            c.execute('INSERT INTO messages(project_id,role,content,created_at) VALUES(?,?,?,?)', (pid, 'assistant', '好的，我们先休息。已经讲述的内容已保存，您随时可以回来继续。', now()))
            return {'ok': True}
        messages = [dict(r) for r in c.execute('SELECT role,content FROM messages WHERE project_id=? ORDER BY id', (pid,))]
        jid = worker.enqueue(c, pid, 'interview', {'messages': messages})
    return {'job_id': jid}


@app.post('/api/projects/{pid}/generate')
def generate(pid: str, current=Depends(user)):
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        p = project_access(c, pid, current)
        idle(c, pid)
        sources = [dict(r) for r in c.execute('SELECT id,name,text,confirmed FROM sources WHERE project_id=? AND included=1 ORDER BY created_at', (pid,))]
        if not sources or any(not s['confirmed'] or not s['text'].strip() for s in sources):
            raise HTTPException(409, '请至少选择一份入书素材，并核对确认全部入书素材；不采用的素材可以取消入书')
        jid = worker.enqueue(c, pid, 'generate', {'revision': p['revision'], 'sources': sources})
        audit(c, pid, current['email'], '提交自传生成任务')
    return {'job_id': jid}


@app.post('/api/projects/{pid}/jobs/{jid}/retry')
def retry(pid: str, jid: str, current=Depends(user)):
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        p = project_access(c, pid, current)
        idle(c, pid)
        job = c.execute("SELECT * FROM jobs WHERE id=? AND project_id=? AND status='failed'", (jid, pid)).fetchone()
        if not job:
            raise HTTPException(409, '只能重试失败任务')
        runtime.check_retry(c, pid, jid)
        payload = json.loads(job['payload'])
        if job['kind'] == 'generate' and payload['revision'] != p['revision']:
            raise HTTPException(409, '素材已经变化，请重新生成新稿')
        if job['kind'] == 'transcribe':
            source = c.execute('SELECT text FROM sources WHERE id=? AND project_id=?', (payload['source_id'], pid)).fetchone()
            if not source or source['text'].strip():
                raise HTTPException(409, '素材已删除或已填写逐字稿，不覆盖现有内容')
        if job['kind'] == 'interview':
            current_messages = [dict(r) for r in c.execute('SELECT role,content FROM messages WHERE project_id=? ORDER BY id', (pid,))]
            if current_messages != payload['messages']:
                raise HTTPException(409, '对话已有新内容，请继续发送新回答')
        c.execute("UPDATE jobs SET status='queued',error='',progress='',cancel_requested=0,updated_at=? WHERE id=?", (now(), jid))
    return {'job_id': jid}


class ChapterUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=100000)
    questions: list[str] = Field(default_factory=list, max_length=100)
    approved: bool = False
    expected_revision: int = Field(ge=0)


@app.put('/api/projects/{pid}/chapters/{cid}')
def update_chapter(pid: str, cid: str, data: ChapterUpdate, current=Depends(user)):
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        p = project_access(c, pid, current)
        row = c.execute('SELECT ch.*,b.revision FROM chapters ch JOIN books b ON b.id=ch.book_id WHERE ch.id=? AND b.project_id=?', (cid, pid)).fetchone()
        if not row:
            raise HTTPException(404, '章节不存在')
        if data.expected_revision != row['edit_revision']:
            raise HTTPException(409, '章节已被其他成员修改，请刷新后核对并重新保存；本地草稿仍保留')
        if data.approved and (row['revision'] != p['revision'] or data.questions):
            raise HTTPException(409, '素材已变化或还有待核实事项，暂不能确认定稿')
        if not data.body.strip() or not data.title.strip():
            raise HTTPException(422, '标题和正文不能为空')
        editorial.snapshot_chapter(c, row, current['email'])
        c.execute('UPDATE releases SET revoked=1 WHERE book_id=?', (row['book_id'],))
        c.execute('UPDATE chapters SET title=?,body=?,questions=?,approved=?,edit_revision=edit_revision+1 WHERE id=?', (data.title, data.body, json.dumps(data.questions, ensure_ascii=False), data.approved, cid))
        audit(c, pid, current['email'], ('确认定稿：' if data.approved else '编辑章节：') + data.title)
    return {'ok': True}


class MemberInput(BaseModel):
    email: str


@app.post('/api/projects/{pid}/members')
def add_member(pid: str, data: MemberInput, current=Depends(user)):
    with connect() as c:
        project_access(c, pid, current, owner=True)
        row = c.execute('SELECT id FROM users WHERE email=?', (data.email.strip().lower(),)).fetchone()
        if not row:
            raise HTTPException(404, '请让协作编辑先注册账户')
        c.execute('INSERT OR IGNORE INTO members VALUES(?,?)', (pid, row['id']))
        audit(c, pid, current['email'], '添加协作编辑：' + data.email)
    return {'ok': True}


@app.delete('/api/projects/{pid}/members/{mid}')
def remove_member(pid: str, mid: str, current=Depends(user)):
    with connect() as c:
        project_access(c, pid, current, owner=True)
        c.execute('DELETE FROM members WHERE project_id=? AND user_id=?', (pid, mid))
        audit(c, pid, current['email'], '移除协作编辑')
    return {'ok': True}


@app.get('/api/projects/{pid}/export')
def export(pid: str, book_id: str, format: Literal['docx', 'zip'] = 'docx', draft: bool = True, current=Depends(user)):
    with connect() as c:
        p = project_access(c, pid, current)
        book = c.execute('SELECT * FROM books WHERE id=? AND project_id=?', (book_id, pid)).fetchone()
        if not book:
            raise HTTPException(404, '稿件不存在')
        chapters = [dict(r) for r in c.execute('SELECT * FROM chapters WHERE book_id=? ORDER BY position', (book_id,))]
        if not chapters:
            raise HTTPException(409, '没有章节可导出')
        if (not draft or format == 'zip') and (book['revision'] != p['revision'] or any(not ch['approved'] for ch in chapters)):
            raise HTTPException(409, '正式交付前请完成全部章节审校；素材改变后需重新生成')
        release = current_release(c, p, book, chapters)
        if (not draft or format == 'zip') and not (release and release['valid']):
            raise HTTPException(409, '正式交付前请由项目创建者登记讲述者对本版书稿及用途的确认')
        sources = [dict(r) for r in c.execute('SELECT id,name,kind FROM sources WHERE project_id=? AND included=1', (pid,))]
        operations.attach_photos(c, p, book_id)
        manifest = None if not release else {
            'book_id': book['id'], 'book_title': p['title'], 'content_sha256': release['content_sha256'],
            'approved_at': release['created_at'], 'approval_scope': release['scope'],
            'intended_audience': '家庭留存' if release['scope'] == 'family' else '出版机构审核（不代表公开发行许可）',
            'chapter_count': len(chapters), 'word_count': sum(len(ch['body']) for ch in chapters),
        }
        logs = [dict(r) for r in c.execute('SELECT actor,action,created_at FROM audit WHERE project_id=? ORDER BY id', (pid,))]
        books = [dict(r) for r in c.execute('SELECT * FROM books WHERE project_id=?', (pid,))]
        audit(c, pid, current['email'], '导出审阅稿' if draft and format == 'docx' else '导出排版交付文件')
    content = bundle(p, chapters, sources, logs, books, release_manifest=manifest) if format == 'zip' else docx_bytes(p, chapters, draft)
    media = 'application/zip' if format == 'zip' else 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    return Response(content, media_type=media, headers={'Content-Disposition': f'attachment; filename="life-story.{format}"'})


@app.delete('/api/projects/{pid}')
def delete_project(pid: str, current=Depends(user)):
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        project_access(c, pid, current, owner=True)
        idle(c, pid)
        asset_paths = [value for r in c.execute('SELECT path,print_path FROM photos WHERE project_id=?', (pid,)) for value in (r['path'],r['print_path'])]
        proof = c.execute('SELECT proof_path FROM print_orders WHERE project_id=?', (pid,)).fetchone()
        if proof and proof['proof_path']: asset_paths.append(proof['proof_path'])
        paths = [r['path'] for r in c.execute('SELECT path FROM sources WHERE project_id=? AND path IS NOT NULL', (pid,))]
        c.execute('DELETE FROM projects WHERE id=?', (pid,))
    for path in paths:
        (config.UPLOADS / path).unlink(missing_ok=True)
    for path in asset_paths:
        (operations.files_dir() / path).unlink(missing_ok=True)
    return {'ok': True}


@app.get('/health')
def health():
    with connect() as c:
        c.execute('SELECT 1')
    return {'status': 'ok'}


class ReleaseInput(BaseModel):
    expected_content_sha256: str = Field(min_length=64, max_length=64)
    scope: Literal['family', 'publication_review']
    signer: str = Field(min_length=1, max_length=80)
    relationship: Literal['self', 'authorized_representative']
    evidence_note: str = Field(min_length=1, max_length=2000)
    confirmed: bool


@app.post('/api/projects/{pid}/books/{bid}/release')
def acknowledge_release(pid: str, bid: str, data: ReleaseInput, current=Depends(user)):
    if not data.confirmed or not data.signer.strip() or not data.evidence_note.strip():
        raise HTTPException(422, '请填写确认人、核对记录并明确确认本版内容和用途')
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        p = project_access(c, pid, current, owner=True)
        idle(c, pid)
        book = c.execute('SELECT * FROM books WHERE id=? AND project_id=?', (bid, pid)).fetchone()
        if not book:
            raise HTTPException(404, '稿件不存在')
        chapters = [dict(r) for r in c.execute('SELECT * FROM chapters WHERE book_id=? ORDER BY position', (bid,))]
        if not chapters or book['revision'] != p['revision'] or any(not ch['approved'] or json.loads(ch['questions']) for ch in chapters):
            raise HTTPException(409, '请先完成当前素材对应的所有章节审校')
        if data.expected_content_sha256 != content_hash(p, book, chapters):
            raise HTTPException(409, '您查看后书稿已有变化，请刷新并重新核对整书再确认')
        # Prior confirmations cannot reactivate if content later happens to match again.
        c.execute('UPDATE releases SET revoked=1 WHERE book_id=?', (bid,))
        rid = uid()
        c.execute('INSERT INTO releases(id,book_id,scope,signer,relationship,evidence_note,actor,content_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                  (rid, bid, data.scope, data.signer.strip(), data.relationship, data.evidence_note.strip(), current['email'], content_hash(p, book, chapters), now()))
        audit(c, pid, current['email'], '登记整书交付确认：' + ('家庭留存' if data.scope == 'family' else '提交出版机构审核'))
    return {'id': rid}


@app.delete('/api/projects/{pid}/books/{bid}/release')
def revoke_release(pid: str, bid: str, current=Depends(user)):
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        project_access(c, pid, current, owner=True)
        if not c.execute('SELECT 1 FROM books WHERE id=? AND project_id=?', (bid, pid)).fetchone():
            raise HTTPException(404, '稿件不存在')
        c.execute('UPDATE releases SET revoked=1 WHERE book_id=?', (bid,))
        audit(c, pid, current['email'], '撤回整书交付确认')
    return {'ok': True}


class ChapterOrder(BaseModel):
    chapter_ids: list[str] = Field(min_length=1, max_length=1000)


@app.post('/api/projects/{pid}/books/{bid}/order')
def order_chapters(pid: str, bid: str, data: ChapterOrder, current=Depends(user)):
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        project_access(c, pid, current)
        if not c.execute('SELECT 1 FROM books WHERE id=? AND project_id=?', (bid, pid)).fetchone():
            raise HTTPException(404, '稿件不存在')
        existing = [r['id'] for r in c.execute('SELECT id FROM chapters WHERE book_id=? ORDER BY position', (bid,))]
        if len(data.chapter_ids) != len(existing) or set(data.chapter_ids) != set(existing):
            raise HTTPException(422, '排序必须恰好包含本版的全部章节，不能重复或跨项目引用')
        if existing != data.chapter_ids:
            for position, cid in enumerate(data.chapter_ids):
                c.execute('UPDATE chapters SET position=? WHERE id=?', (position, cid))
            c.execute('UPDATE releases SET revoked=1 WHERE book_id=?', (bid,))
            audit(c, pid, current['email'], '调整章节顺序；需重新确认整书交付')
    return {'ok': True}


editorial.register(app, user, project_access, idle)
runtime.register(app, user, project_access, idle)
operations.register(app, user, project_access, idle)

accounts.register(app, throttle, hash_password)
