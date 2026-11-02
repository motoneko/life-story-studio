"""Email-based password recovery. No reset token is returned to API clients or logs."""
import hashlib
import os
import secrets
import smtplib
import ssl
import time
from email.message import EmailMessage
from fastapi import HTTPException
from pydantic import BaseModel, Field
from .db import connect


def init_schema(c):
    c.execute('CREATE TABLE IF NOT EXISTS password_resets(token_hash TEXT PRIMARY KEY,user_id TEXT REFERENCES users(id) ON DELETE CASCADE,expires REAL NOT NULL)')


class ResetRequest(BaseModel):
    email: str = Field(min_length=3,max_length=180)


class ResetPassword(BaseModel):
    token: str = Field(min_length=20,max_length=200)
    password: str = Field(min_length=10,max_length=128)


def send_reset(address, token):
    origin=os.getenv('PUBLIC_BASE_URL','').rstrip('/')
    if not os.getenv('SMTP_HOST') or not origin.startswith('https://'):
        raise HTTPException(503,'尚未配置邮件恢复，请联系部署方进行账户恢复')
    message=EmailMessage(); message['Subject']='拾光：重设账户密码'; message['From']=os.getenv('SMTP_FROM',os.getenv('SMTP_USER','')); message['To']=address
    message.set_content('请打开以下链接重设密码，30分钟内有效。若非本人操作可忽略。\n'+origin+'/#reset='+token)
    # STARTTLS is mandatory for network SMTP; no credentials in request URLs or logs.
    with smtplib.SMTP(os.environ['SMTP_HOST'],int(os.getenv('SMTP_PORT','587')),timeout=20) as server:
        server.starttls(context=ssl.create_default_context())
        if os.getenv('SMTP_USER'):server.login(os.environ['SMTP_USER'],os.getenv('SMTP_PASSWORD',''))
        server.send_message(message)


def register(app, throttle, hash_password):
    from fastapi import Request
    @app.post('/api/password/request')
    def request_reset(data:ResetRequest, request:Request):
        throttle(request)
        if not os.getenv('SMTP_HOST') or not os.getenv('PUBLIC_BASE_URL','').startswith('https://'):
            raise HTTPException(503,'尚未配置邮件恢复，请联系部署方进行账户恢复')
        with connect() as c:
            row=c.execute('SELECT id,email FROM users WHERE email=?',(data.email.strip().lower(),)).fetchone()
        if row:
            token=secrets.token_urlsafe(32); digest=hashlib.sha256(token.encode()).hexdigest()
            with connect() as c:
                c.execute('DELETE FROM password_resets WHERE expires<? OR user_id=?',(time.time(),row['id']))
                c.execute('INSERT INTO password_resets VALUES(?,?,?)',(digest,row['id'],time.time()+1800))
            try:send_reset(row['email'],token)
            except Exception:
                with connect() as c:c.execute('DELETE FROM password_resets WHERE token_hash=?',(digest,))
                # Same public response prevents enumeration when provider rejects an address.
        return {'message':'如该邮箱已注册且邮件服务正常，将收到恢复链接。'}

    @app.post('/api/password/reset')
    def reset(data:ResetPassword,request:Request):
        throttle(request)
        with connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT user_id FROM password_resets WHERE token_hash=? AND expires>?',(hashlib.sha256(data.token.encode()).hexdigest(),time.time())).fetchone()
            if not row:raise HTTPException(400,'恢复链接已失效或已使用')
            c.execute('UPDATE users SET password=? WHERE id=?',(hash_password(data.password),row['user_id']))
            c.execute('DELETE FROM sessions WHERE user_id=?',(row['user_id'],))
            c.execute('DELETE FROM password_resets WHERE user_id=?',(row['user_id'],))
        return {'ok':True}
