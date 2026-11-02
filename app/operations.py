"""Private media archive, editorial brief, controlled audio sharing and print handoff."""
import hashlib
import io
import json
import secrets
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Literal
from fastapi import Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from PIL import Image, ImageOps, UnidentifiedImageError
from . import config
from .db import connect, now, uid, audit
from .release import current_release, content_hash


def init_schema(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS briefs(project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,content TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS photos(id TEXT PRIMARY KEY,project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,path TEXT NOT NULL,print_path TEXT NOT NULL,caption TEXT NOT NULL,taken_at TEXT NOT NULL,people TEXT NOT NULL,width INTEGER,height INTEGER,consent INTEGER NOT NULL,included INTEGER NOT NULL DEFAULT 0,chapter_id TEXT,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS print_orders(project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,spec TEXT NOT NULL,stage TEXT NOT NULL DEFAULT 'editing',revision INTEGER NOT NULL DEFAULT 0,book_id TEXT,proof_path TEXT,proof_hash TEXT,proof_book_hash TEXT,proof_approved_by TEXT,proof_approved_at TEXT,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS audio_shares(id TEXT PRIMARY KEY,project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,source_id TEXT REFERENCES sources(id) ON DELETE CASCADE,token_hash TEXT NOT NULL,expires REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL);
    ''')
    if 'delivery_revision' not in {r['name'] for r in c.execute('PRAGMA table_info(projects)')}:
        c.execute('ALTER TABLE projects ADD COLUMN delivery_revision INTEGER NOT NULL DEFAULT 0')


def files_dir():
    directory = config.UPLOADS / 'assets'
    directory.mkdir(exist_ok=True)
    return directory


def touch(c, pid):
    c.execute('UPDATE projects SET delivery_revision=delivery_revision+1 WHERE id=?', (pid,))
    c.execute('UPDATE releases SET revoked=1 WHERE book_id IN (SELECT id FROM books WHERE project_id=?)', (pid,))
    c.execute('UPDATE print_orders SET proof_approved_by=NULL,proof_approved_at=NULL WHERE project_id=?', (pid,))


def get_state(c, pid):
    brief = c.execute('SELECT * FROM briefs WHERE project_id=?', (pid,)).fetchone()
    order = c.execute('SELECT * FROM print_orders WHERE project_id=?', (pid,)).fetchone()
    return {'brief': {'content': json.loads(brief['content']), 'revision': brief['revision']} if brief else {'content': {}, 'revision': 0},
            'photos': [dict(r) for r in c.execute('SELECT id,caption,taken_at,people,width,height,consent,included,chapter_id FROM photos WHERE project_id=? ORDER BY created_at', (pid,))],
            'order': None if not order else {k: order[k] for k in ('spec','stage','revision','book_id','proof_hash','proof_book_hash','proof_approved_by','proof_approved_at','updated_at')},
            'shares': [dict(r) for r in c.execute('SELECT id,source_id,expires,revoked FROM audio_shares WHERE project_id=?', (pid,))]}


def attach_photos(c, p, book_id):
    p['_photos'] = [{**dict(r), 'file': str(files_dir() / r['print_path'])} for r in c.execute('SELECT * FROM photos WHERE project_id=? AND consent=1 AND included=1 AND (chapter_id IS NULL OR chapter_id IN (SELECT id FROM chapters WHERE book_id=?))', (p['id'], book_id))]


class Brief(BaseModel):
    expected_revision: int = Field(ge=0)
    dialect: str = Field(default='', max_length=100)
    interviewer: str = Field(default='', max_length=100)
    interview_date: str = Field(default='', max_length=50)
    audience: str = Field(default='家人', max_length=200)
    target_words: int = Field(default=20000, ge=1000, le=300000)
    taboo_topics: str = Field(default='', max_length=2000)
    aliases: str = Field(default='', max_length=2000)
    narrator_preferences: str = Field(default='', max_length=2000)


class PhotoUpdate(BaseModel):
    caption: str = Field(default='', max_length=500)
    taken_at: str = Field(default='', max_length=100)
    people: str = Field(default='', max_length=500)
    consent: bool = False
    included: bool = False
    chapter_id: str | None = None


class OrderInput(BaseModel):
    expected_revision: int = Field(ge=0)
    stage: Literal['editing','layout','proof','approved','printed','shipped','delivered','cancelled'] = 'editing'
    book_id: str
    size: str = Field(default='A5', max_length=100)
    binding: str = Field(default='平装', max_length=100)
    paper: str = Field(default='待确认', max_length=100)
    copies: int = Field(default=1, ge=1, le=10000)
    revision_rounds: int = Field(default=2, ge=0, le=100)
    terms: str = Field(default='', max_length=5000)
    tracking: str = Field(default='', max_length=500)


class ProofApproval(BaseModel):
    expected_revision: int
    proof_hash: str
    signer: str = Field(min_length=1, max_length=100)
    confirmed: bool


class ShareInput(BaseModel):
    source_id: str
    hours: int = Field(default=24, ge=1, le=168)
    consent: bool


def register(app, user, project_access, idle):
    base = '/api/projects/{pid}/operations'

    @app.get(base)
    def state(pid: str, current=Depends(user)):
        with connect() as c:
            project_access(c, pid, current)
            return get_state(c, pid)

    @app.put(base + '/brief')
    def brief(pid: str, data: Brief, current=Depends(user)):
        with connect() as c:
            c.execute('BEGIN IMMEDIATE')
            project_access(c, pid, current)
            idle(c, pid)
            old = c.execute('SELECT revision FROM briefs WHERE project_id=?', (pid,)).fetchone()
            if data.expected_revision != (old['revision'] if old else 0):
                raise HTTPException(409, '采访任务单已变化，请刷新后重试')
            content = data.model_dump(exclude={'expected_revision'})
            c.execute('INSERT INTO briefs VALUES(?,?,?) ON CONFLICT(project_id) DO UPDATE SET content=excluded.content,revision=excluded.revision', (pid, json.dumps(content, ensure_ascii=False), data.expected_revision + 1))
            c.execute('UPDATE projects SET revision=revision+1 WHERE id=?', (pid,))
            audit(c, pid, current['email'], '更新采访任务单和写作要求')
        return {'ok': True}

    @app.post(base + '/photos')
    async def photo_upload(pid: str, file: UploadFile = File(...), current=Depends(user)):
        with connect() as c:
            project_access(c, pid, current)
            idle(c, pid)
            if c.execute('SELECT count(*) FROM photos WHERE project_id=?', (pid,)).fetchone()[0] >= 100:
                raise HTTPException(409, '每个项目最多保存 100 张照片')
        data = await file.read(10 * 1024 * 1024 + 1)
        await file.close()
        if len(data) > 10 * 1024 * 1024:
            raise HTTPException(413, '单张照片不能超过 10 MB')
        try:
            with Image.open(io.BytesIO(data)) as im:
                if im.format not in ('JPEG','PNG','WEBP') or im.width * im.height > 30_000_000:
                    raise ValueError()
                im.load()
                image = ImageOps.exif_transpose(im).convert('RGB')
                width, height = image.size
                clean = io.BytesIO()
                image.save(clean, format='JPEG', quality=95)
        except (UnidentifiedImageError, ValueError, OSError, Image.DecompressionBombError) as e:
            raise HTTPException(422, '请选择有效的 JPEG/PNG/WEBP，最大 3000 万像素') from e
        fid = uid()
        original, printed = files_dir() / (fid + '.original'), files_dir() / (fid + '.jpg')
        try:
            original.write_bytes(data)
            printed.write_bytes(clean.getvalue())
            with connect() as c:
                c.execute('BEGIN IMMEDIATE')
                project_access(c, pid, current)
                idle(c, pid)
                c.execute('INSERT INTO photos VALUES(?,?,?,?,?,?,?,?,?,?,0,NULL,?)', (fid,pid,original.name,printed.name,'','','',width,height,0,now()))
                audit(c,pid,current['email'],'保存照片，等待说明与使用确认')
        except BaseException:
            original.unlink(missing_ok=True); printed.unlink(missing_ok=True)
            raise
        return {'id': fid, 'width': width, 'height': height, 'print_width_cm_at_300dpi': round(width / 300 * 2.54, 1)}

    @app.put(base + '/photos/{fid}')
    def photo_update(pid: str, fid: str, data: PhotoUpdate, current=Depends(user)):
        if data.included and not data.consent:
            raise HTTPException(422, '入书前请确认照片使用权及相关人物的使用范围')
        with connect() as c:
            c.execute('BEGIN IMMEDIATE'); project_access(c,pid,current); idle(c,pid)
            if not c.execute('SELECT 1 FROM photos WHERE id=? AND project_id=?',(fid,pid)).fetchone():
                raise HTTPException(404, '照片不存在')
            if data.chapter_id and not c.execute('SELECT 1 FROM chapters ch JOIN books b ON b.id=ch.book_id WHERE ch.id=? AND b.project_id=?',(data.chapter_id,pid)).fetchone():
                raise HTTPException(422, '照片关联章节不属于本项目')
            c.execute('UPDATE photos SET caption=?,taken_at=?,people=?,consent=?,included=?,chapter_id=? WHERE id=?', (data.caption,data.taken_at,data.people,data.consent,data.included,data.chapter_id,fid))
            touch(c,pid); audit(c,pid,current['email'],'更新照片说明与入书范围')
        return {'ok':True}

    @app.get(base + '/photos/{fid}')
    def photo_file(pid: str, fid: str, original: bool=False, current=Depends(user)):
        with connect() as c:
            project_access(c,pid,current,owner=original)
            row=c.execute('SELECT * FROM photos WHERE id=? AND project_id=?',(fid,pid)).fetchone()
        if not row: raise HTTPException(404,'照片不存在')
        return FileResponse(files_dir()/row['path' if original else 'print_path'],media_type='application/octet-stream' if original else 'image/jpeg',filename=fid+('.original' if original else '.jpg'))

    @app.delete(base + '/photos/{fid}')
    def photo_delete(pid: str, fid: str, current=Depends(user)):
        with connect() as c:
            c.execute('BEGIN IMMEDIATE'); project_access(c,pid,current); idle(c,pid)
            row=c.execute('SELECT * FROM photos WHERE id=? AND project_id=?',(fid,pid)).fetchone()
            if not row: raise HTTPException(404,'照片不存在')
            c.execute('DELETE FROM photos WHERE id=?',(fid,)); touch(c,pid)
        for key in ('path','print_path'): (files_dir()/row[key]).unlink(missing_ok=True)
        return {'ok':True}

    @app.put(base + '/order')
    def order(pid: str, data: OrderInput, current=Depends(user)):
        stale_proof = None
        with connect() as c:
            c.execute('BEGIN IMMEDIATE'); p=project_access(c,pid,current,owner=True)
            old=c.execute('SELECT * FROM print_orders WHERE project_id=?',(pid,)).fetchone()
            if data.expected_revision != (old['revision'] if old else 0): raise HTTPException(409,'订单已更新，请刷新')
            book=c.execute('SELECT * FROM books WHERE id=? AND project_id=?',(data.book_id,pid)).fetchone()
            if not book: raise HTTPException(404,'稿件不存在')
            chapters=[dict(r) for r in c.execute('SELECT * FROM chapters WHERE book_id=? ORDER BY position',(data.book_id,))]
            spec=json.dumps(data.model_dump(exclude={'expected_revision','stage','book_id'}),ensure_ascii=False,sort_keys=True)
            unchanged=old and {k:v for k,v in json.loads(old['spec']).items() if k!='tracking'}=={k:v for k,v in json.loads(spec).items() if k!='tracking'} and old['book_id']==data.book_id
            if data.stage in ('approved','printed','shipped','delivered'):
                release=current_release(c,p,book,chapters)
                if not unchanged or not old['proof_approved_by'] or old['proof_book_hash']!=content_hash(p,book,chapters) or not release or not release['valid']:
                    raise HTTPException(409,'请先确认当前文稿及当前规格对应的排版校样')
            c.execute('INSERT INTO print_orders(project_id,spec,stage,revision,book_id,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET spec=excluded.spec,stage=excluded.stage,revision=excluded.revision,book_id=excluded.book_id,updated_at=excluded.updated_at', (pid,spec,data.stage,data.expected_revision+1,data.book_id,now()))
            if not unchanged:
                # A PDF is evidence for one book and one set of production terms.
                # Clearing only its approval would let the same old PDF/hash be
                # acknowledged again against the newly changed order revision.
                stale_proof = old['proof_path'] if old else None
                c.execute('UPDATE print_orders SET proof_path=NULL,proof_hash=NULL,proof_book_hash=NULL,proof_approved_by=NULL,proof_approved_at=NULL WHERE project_id=?',(pid,))
            audit(c,pid,current['email'],'更新线下交付订单：'+data.stage)
        # Remove the previous PDF only after the changed order has committed.
        if stale_proof:
            (files_dir()/stale_proof).unlink(missing_ok=True)
        return {'ok':True}

    @app.post(base + '/proof')
    async def proof(pid: str, file: UploadFile=File(...), current=Depends(user)):
        with connect() as c:
            p=project_access(c,pid,current,owner=True)
            order=c.execute('SELECT * FROM print_orders WHERE project_id=?',(pid,)).fetchone()
            if not order: raise HTTPException(409,'请先创建交付订单')
            book=c.execute('SELECT * FROM books WHERE id=?',(order['book_id'],)).fetchone()
            chapters=[dict(r) for r in c.execute('SELECT * FROM chapters WHERE book_id=? ORDER BY position',(book['id'],))]
            digest=content_hash(p,book,chapters)
        raw=await file.read(20*1024*1024+1); await file.close()
        if len(raw)>20*1024*1024: raise HTTPException(413,'校样最大20MB')
        if not raw.startswith(b'%PDF-'): raise HTTPException(422,'请上传PDF校样')
        path=files_dir()/(uid()+'.pdf'); path.write_bytes(raw)
        try:
            with connect() as c:
                c.execute('BEGIN IMMEDIATE'); p=project_access(c,pid,current,owner=True)
                latest=c.execute('SELECT * FROM print_orders WHERE project_id=?',(pid,)).fetchone()
                if not latest or latest['revision']!=order['revision']: raise HTTPException(409,'上传期间订单已变化，请重新上传')
                chapters=[dict(r) for r in c.execute('SELECT * FROM chapters WHERE book_id=? ORDER BY position',(book['id'],))]
                if content_hash(p,book,chapters)!=digest: raise HTTPException(409,'上传期间书稿已变化')
                c.execute("UPDATE print_orders SET proof_path=?,proof_hash=?,proof_book_hash=?,proof_approved_by=NULL,proof_approved_at=NULL,revision=revision+1,stage='proof',updated_at=? WHERE project_id=?",(path.name,hashlib.sha256(raw).hexdigest(),digest,now(),pid))
        except BaseException: path.unlink(missing_ok=True); raise
        if order['proof_path']: (files_dir()/order['proof_path']).unlink(missing_ok=True)
        return {'ok':True}

    @app.get(base + '/proof')
    def proof_download(pid: str,current=Depends(user)):
        with connect() as c:
            project_access(c,pid,current)
            row=c.execute('SELECT proof_path FROM print_orders WHERE project_id=?',(pid,)).fetchone()
        if not row or not row['proof_path']: raise HTTPException(404,'没有校样')
        return FileResponse(files_dir()/row['proof_path'],media_type='application/pdf',filename='proof.pdf')

    @app.post(base + '/proof/approve')
    def approve(pid: str,data:ProofApproval,current=Depends(user)):
        if not data.confirmed or not data.signer.strip(): raise HTTPException(422,'请明确确认校样及确认人')
        with connect() as c:
            c.execute('BEGIN IMMEDIATE'); p=project_access(c,pid,current,owner=True)
            order=c.execute('SELECT * FROM print_orders WHERE project_id=?',(pid,)).fetchone()
            if not order or not order['proof_path'] or order['revision']!=data.expected_revision or order['proof_hash']!=data.proof_hash: raise HTTPException(409,'校样版本已变化')
            book=c.execute('SELECT * FROM books WHERE id=?',(order['book_id'],)).fetchone()
            chapters=[dict(r) for r in c.execute('SELECT * FROM chapters WHERE book_id=? ORDER BY position',(book['id'],))]
            release=current_release(c,p,book,chapters)
            if not release or not release['valid'] or order['proof_book_hash']!=content_hash(p,book,chapters): raise HTTPException(409,'文稿已变化或尚未确认交付')
            c.execute("UPDATE print_orders SET proof_approved_by=?,proof_approved_at=?,stage='approved',revision=revision+1 WHERE project_id=?",(data.signer.strip(),now(),pid)); audit(c,pid,current['email'],'登记排版校样确认')
        return {'ok':True}

    @app.post(base + '/shares')
    def share(pid:str,data:ShareInput,current=Depends(user)):
        if not data.consent: raise HTTPException(422,'请确认允许持链接的人在有效期内访问原声')
        token=secrets.token_urlsafe(32); sid=uid()
        with connect() as c:
            project_access(c,pid,current,owner=True)
            if not c.execute('SELECT 1 FROM sources WHERE id=? AND project_id=? AND path IS NOT NULL',(data.source_id,pid)).fetchone(): raise HTTPException(404,'录音不存在')
            c.execute('INSERT INTO audio_shares VALUES(?,?,?,?,?,0,?)',(sid,pid,data.source_id,hashlib.sha256(token.encode()).hexdigest(),time.time()+data.hours*3600,now())); audit(c,pid,current['email'],'创建限时原声分享')
        return {'id':sid,'path':'/api/shared-audio/'+token,'qr_path':'/api/shared-audio/'+token+'/qr','expires_in_hours':data.hours}

    @app.delete(base + '/shares/{sid}')
    def revoke(pid:str,sid:str,current=Depends(user)):
        with connect() as c:
            project_access(c,pid,current,owner=True)
            c.execute('UPDATE audio_shares SET revoked=1 WHERE id=? AND project_id=?',(sid,pid))
        return {'ok':True}

    def shared(token):
        with connect() as c:
            row=c.execute('SELECT s.path FROM audio_shares a JOIN sources s ON s.id=a.source_id WHERE a.token_hash=? AND a.expires>? AND a.revoked=0',(hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()
        if not row: raise HTTPException(404,'分享不存在、已过期或已撤回')
        return row

    @app.get('/api/shared-audio/{token}')
    def shared_audio(token:str):
        row=shared(token)
        return FileResponse(config.UPLOADS/row['path'],filename='shared-recording'+Path(row['path']).suffix,headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})

    @app.get('/api/shared-audio/{token}/qr')
    def share_qr(token:str):
        import os,qrcode
        shared(token)
        origin=os.getenv('PUBLIC_BASE_URL','').rstrip('/')
        if not origin.startswith('https://'): raise HTTPException(409,'请配置HTTPS PUBLIC_BASE_URL后生成可扫码的链接')
        output=io.BytesIO(); qrcode.make(origin+'/api/shared-audio/'+token).save(output,format='PNG')
        return Response(output.getvalue(),media_type='image/png',headers={'Cache-Control':'no-store'})

    @app.get(base + '/archive')
    def archive(pid:str,include_audio:bool=False,current=Depends(user)):
        with connect() as c:
            c.execute('BEGIN'); p=project_access(c,pid,current,owner=True); idle(c,pid)
            tables={}
            for table in ('sources','messages','books','photos','briefs','print_orders','audit','editorial_plans','editorial_checks','project_runtime','usage_entries','project_rates'):
                tables[table]=[dict(r) for r in c.execute(f'SELECT * FROM {table} WHERE project_id=?',(pid,))]
            tables['chapters']=[dict(r) for r in c.execute('SELECT ch.* FROM chapters ch JOIN books b ON b.id=ch.book_id WHERE b.project_id=?',(pid,))]
            tables['chapter_history']=[dict(r) for r in c.execute('SELECT h.* FROM chapter_history h JOIN chapters ch ON ch.id=h.chapter_id JOIN books b ON b.id=ch.book_id WHERE b.project_id=?',(pid,))]
            tables['releases']=[dict(r) for r in c.execute('SELECT r.* FROM releases r JOIN books b ON b.id=r.book_id WHERE b.project_id=?',(pid,))]
            assets=[]
            if include_audio:
                assets += [(config.UPLOADS/s['path'],'audio/'+s['path']) for s in tables['sources'] if s['path']]
            assets += [(files_dir()/s['path'],'photos/'+s['id']+'.original') for s in tables['photos']]
            if sum(path.stat().st_size for path,_ in assets if path.exists())>512*1024*1024: raise HTTPException(413,'档案包超过512MB，请选择不含音频并逐份下载录音')
            handle=tempfile.NamedTemporaryFile(suffix='.zip',delete=False); path=Path(handle.name); handle.close()
            try:
                with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:
                    z.writestr('project.json',json.dumps({'project':p,'tables':tables},ensure_ascii=False,indent=2))
                    z.writestr('PRIVATE.txt','私人档案包含原始采访及历史内容，仅供本人保存；请勿作为排版交付包转发。不包含账户密码、登录令牌或模型密钥。')
                    for asset,name in assets:
                        if asset.exists():z.write(asset,name)
            except BaseException: path.unlink(missing_ok=True);raise
        return FileResponse(path,filename='private-life-archive.zip',background=BackgroundTask(path.unlink,missing_ok=True))
