import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
DATA = Path(os.getenv('DATA_DIR', './data')).resolve()
DATA.mkdir(parents=True, exist_ok=True)
UPLOADS = DATA / 'uploads'
UPLOADS.mkdir(exist_ok=True)
DB = DATA / 'studio.sqlite3'
AI_MODE = os.getenv('AI_MODE', 'demo')
if AI_MODE not in ('demo', 'live'):
    raise RuntimeError('AI_MODE 只能为 demo 或 live')
MAX_UPLOAD = int(os.getenv('MAX_UPLOAD_MB', '200')) * 1024 * 1024
COOKIE_SECURE = os.getenv('COOKIE_SECURE', 'false').lower() == 'true'
