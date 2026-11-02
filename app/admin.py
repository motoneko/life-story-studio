"""Explicit offline administrator tools. Never pass passwords as shell arguments."""
import argparse
import getpass
import shutil
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path
from . import config
from .db import connect, init


def backup(destination):
    destination=Path(destination).resolve()
    uploads=config.UPLOADS.resolve()
    if destination.is_relative_to(uploads):raise RuntimeError('备份目标不能位于素材目录中')
    if destination.exists():raise RuntimeError('目标备份已存在，不覆盖')
    files=list(uploads.rglob('*'))
    if any(file.is_symlink() for file in files):raise RuntimeError('素材目录包含符号链接，拒绝备份')
    with connect() as c:
        lease=c.execute('SELECT 1 FROM worker_lease WHERE expires>?',(time.time(),)).fetchone()
        if lease:raise RuntimeError('请先停止任务worker和Web服务再备份，保证音频与数据库一致')
    with tempfile.TemporaryDirectory() as tmp:
        target=sqlite3.connect(str(Path(tmp)/'studio.sqlite3'))
        source=sqlite3.connect(config.DB)
        source.backup(target);source.close();target.close()
        with zipfile.ZipFile(destination,'x',zipfile.ZIP_DEFLATED) as z:
            z.write(Path(tmp)/'studio.sqlite3','studio.sqlite3')
            for file in files:
                if file.is_file():z.write(file,'uploads/'+file.relative_to(uploads).as_posix())
    return destination


def restore(archive,destination):
    destination=Path(destination).resolve()
    if destination.exists() and any(destination.iterdir()):raise RuntimeError('仅允许恢复到空目录')
    destination.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        for member in z.infolist():
            path=(destination/member.filename).resolve()
            if not path.is_relative_to(destination):raise RuntimeError('非法归档路径')
            if (member.external_attr >> 16) & 0o170000 == 0o120000:raise RuntimeError('不允许符号链接')
        if 'studio.sqlite3' not in z.namelist():raise RuntimeError('缺少数据库')
        z.extractall(destination)
    conn=sqlite3.connect(destination/'studio.sqlite3')
    result=conn.execute('PRAGMA integrity_check').fetchone()[0];conn.close()
    if result!='ok':raise RuntimeError('备份数据库完整性检查失败')
    return destination


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    b=sub.add_parser('backup');b.add_argument('destination')
    r=sub.add_parser('restore');r.add_argument('archive');r.add_argument('destination')
    a=sub.add_parser('reset-password');a.add_argument('email')
    args=p.parse_args()
    if args.command=='restore':print(restore(args.archive,args.destination));return
    init()
    if args.command=='backup':print(backup(args.destination));return
    password=getpass.getpass('新密码（至少10位）：')
    if len(password)<10 or password!=getpass.getpass('再次输入：'):raise RuntimeError('密码不符合要求或两次不一致')
    from .main import hash_password
    with connect() as c:
        row=c.execute('SELECT id FROM users WHERE email=?',(args.email.strip().lower(),)).fetchone()
        if not row:raise RuntimeError('账户不存在')
        c.execute('UPDATE users SET password=? WHERE id=?',(hash_password(password),row['id']))
        c.execute('DELETE FROM sessions WHERE user_id=?',(row['id'],))
        c.execute('DELETE FROM password_resets WHERE user_id=?',(row['id'],))
    print('密码已重设，旧会话已失效。')

if __name__=='__main__':main()
