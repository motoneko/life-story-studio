import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4
from .config import DB


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid4().hex


@contextmanager
def connect():
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init():
    with connect() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,email TEXT UNIQUE NOT NULL,password TEXT NOT NULL,name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id TEXT REFERENCES users(id) ON DELETE CASCADE,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,owner_id TEXT REFERENCES users(id),name TEXT NOT NULL,title TEXT NOT NULL,birth_year TEXT,style TEXT NOT NULL,consent TEXT NOT NULL,created_at TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS members(project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,user_id TEXT REFERENCES users(id),PRIMARY KEY(project_id,user_id));
        CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,kind TEXT NOT NULL,name TEXT NOT NULL,text TEXT NOT NULL DEFAULT '',segments TEXT NOT NULL DEFAULT '[]',path TEXT,confirmed INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,role TEXT NOT NULL,content TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS books(id TEXT PRIMARY KEY,project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,revision INTEGER NOT NULL,created_at TEXT NOT NULL,mode TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS chapters(id TEXT PRIMARY KEY,book_id TEXT REFERENCES books(id) ON DELETE CASCADE,position INTEGER,title TEXT NOT NULL,body TEXT NOT NULL,source_ids TEXT NOT NULL,questions TEXT NOT NULL,approved INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,kind TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL,error TEXT NOT NULL DEFAULT '',progress TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,actor TEXT NOT NULL,action TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS releases(id TEXT PRIMARY KEY,book_id TEXT REFERENCES books(id) ON DELETE CASCADE,scope TEXT NOT NULL,signer TEXT NOT NULL,relationship TEXT NOT NULL,evidence_note TEXT NOT NULL,actor TEXT NOT NULL,content_sha256 TEXT NOT NULL,created_at TEXT NOT NULL,revoked INTEGER NOT NULL DEFAULT 0);
        ''')
        c.execute('CREATE TABLE IF NOT EXISTS upload_receipts(project_id TEXT REFERENCES projects(id) ON DELETE CASCADE, recording_id TEXT, source_id TEXT REFERENCES sources(id) ON DELETE CASCADE, job_id TEXT REFERENCES jobs(id) ON DELETE CASCADE, PRIMARY KEY(project_id,recording_id))')
        # Additive migrations preserve existing local databases and can be rerun.
        for table, column, definition in (
            ('sources', 'included', 'INTEGER NOT NULL DEFAULT 1'),
            ('chapters', 'edit_revision', 'INTEGER NOT NULL DEFAULT 0'),
        ):
            columns = {row['name'] for row in c.execute(f'PRAGMA table_info({table})')}
            if column not in columns:
                c.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
        from . import editorial, runtime, operations, accounts
        editorial.init_schema(c)
        runtime.init_schema(c)
        operations.init_schema(c)
        accounts.init_schema(c)
        c.execute('PRAGMA user_version=3')


def audit(c, project, actor, action):
    c.execute('INSERT INTO audit(project_id,actor,action,created_at) VALUES(?,?,?,?)', (project, actor, action, now()))
