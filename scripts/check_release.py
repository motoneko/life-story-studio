"""Check only tracked source files; never print potential secret values."""
import re
import subprocess
from pathlib import Path

files=subprocess.check_output(['git','ls-files','-z']).decode().split('\0')
problems=[]
for name in filter(None,files):
    path=Path(name)
    if name.startswith(('data/','.venv/','backups/')) or (path.name.startswith('.env') and name!='.env.example') or path.suffix in ('.sqlite3','.db','.pem','.key'):
        problems.append((name,'private/runtime file'))
    if path.suffix.lower() in ('.png','.jpg','.jpeg','.webp'):continue
    text=path.read_text(errors='replace')
    patterns=[r'gh[pousr]_[A-Za-z0-9]{20,}',r'github_pat_[A-Za-z0-9_]{30,}',r'sk-[A-Za-z0-9_-]{24,}',r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----']
    if any(re.search(pattern,text) for pattern in patterns):problems.append((name,'potential secret'))
if problems:
    for path,reason in problems:print(path,reason)
    raise SystemExit(1)
print(f'PASS: {sum(bool(name) for name in files)} tracked files; no runtime database, private env or matching credential patterns')
