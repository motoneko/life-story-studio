"""Version-bound delivery acknowledgement, not electronic identity or publication licensing."""
import hashlib
import json


def content_hash(project, book, chapters):
    snapshot = {
        'project_id': project['id'], 'book_id': book['id'],
        'title': project['title'], 'narrator': project['name'], 'delivery_revision': project.get('delivery_revision',0),
        'source_revision': project['revision'], 'book_revision': book['revision'],
        'chapters': [{k: ch[k] for k in ('id', 'position', 'title', 'body', 'source_ids', 'questions', 'approved', 'edit_revision')} for ch in chapters],
    }
    return hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def current_release(c, project, book, chapters):
    row = c.execute('SELECT * FROM releases WHERE book_id=? ORDER BY rowid DESC LIMIT 1', (book['id'],)).fetchone()
    if not row:
        return None
    result = dict(row)
    result['valid'] = (not result['revoked'] and bool(chapters)
                       and book['revision'] == project['revision']
                       and all(ch['approved'] for ch in chapters)
                       and result['content_sha256'] == content_hash(project, book, chapters))
    return result
