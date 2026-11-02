import io
import json
import zipfile
import pytest
from test_workflow import client, project, text_source, finish_jobs
from app import db


def make_book(client, pid, count=1):
    for _ in range(count):
        text_source(client, pid)
    assert client.post(f'/api/projects/{pid}/generate').status_code == 200
    finish_jobs(client, pid)
    book = client.get(f'/api/projects/{pid}').json()['books'][0]
    for chapter in book['chapters']:
        assert client.put(f'/api/projects/{pid}/chapters/{chapter["id"]}', json={
            'title': chapter['title'], 'body': chapter['body'], 'questions': [], 'approved': True,
            'expected_revision': chapter['edit_revision'],
        }).status_code == 200
    return client.get(f'/api/projects/{pid}').json()['books'][0]


def acknowledge(client, pid, bid):
    return client.post(f'/api/projects/{pid}/books/{bid}/release', json={
        'expected_content_sha256': next(b for b in client.get(f'/api/projects/{pid}').json()['books'] if b['id'] == bid)['content_sha256'],
        'scope': 'family', 'signer': '陈奶奶', 'relationship': 'self',
        'evidence_note': '2026-11-01 当面通读本版本，确认家庭留存。', 'confirmed': True,
    })


def test_excluded_material_does_not_block_or_enter_generation(client):
    pid = project(client)
    sid = client.post(f'/api/projects/{pid}/sources', json={'name': '家中私事', 'text': 'EXCLUDED_SECRET'}).json()['id']
    assert client.put(f'/api/projects/{pid}/sources/{sid}', json={'name': '家中私事', 'text': 'EXCLUDED_SECRET', 'included': False}).status_code == 200
    book = make_book(client, pid)
    assert len(book['chapters']) == 1
    assert 'EXCLUDED_SECRET' not in json.dumps(book)
    assert acknowledge(client, pid, book['id']).status_code == 200
    r = client.get(f'/api/projects/{pid}/export?book_id={book["id"]}&format=zip')
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        assert b'EXCLUDED_SECRET' not in b''.join(z.read(n) for n in z.namelist())
        assert all(s['id'] != sid for s in json.loads(z.read('source-index.json')))
        assert json.loads(z.read('release-manifest.json'))['approval_scope'] == 'family'
    # Re-including previously unconfirmed material invalidates formal release.
    client.put(f'/api/projects/{pid}/sources/{sid}', json={'name': '家中私事', 'text': 'EXCLUDED_SECRET', 'included': True})
    assert client.get(f'/api/projects/{pid}/export?book_id={book["id"]}&format=zip').status_code == 409


def test_release_binds_content_order_and_explicit_revocation(client):
    pid = project(client)
    book = make_book(client, pid, count=2)
    url = f'/api/projects/{pid}/export?book_id={book["id"]}&draft=false'
    assert client.get(url).status_code == 409
    assert acknowledge(client, pid, book['id']).status_code == 200
    assert client.get(url).status_code == 200
    ids = [ch['id'] for ch in book['chapters']][::-1]
    assert client.post(f'/api/projects/{pid}/books/{book["id"]}/order', json={'chapter_ids': ids}).status_code == 200
    assert client.get(url).status_code == 409
    assert acknowledge(client, pid, book['id']).status_code == 200
    assert client.delete(f'/api/projects/{pid}/books/{book["id"]}/release').status_code == 200
    assert client.get(url).status_code == 409
    assert acknowledge(client, pid, book['id']).status_code == 200
    ch = book['chapters'][0]
    update = {'title': ch['title'], 'body': ch['body']+'补充一句。', 'questions': [], 'approved': True, 'expected_revision': ch['edit_revision']}
    assert client.put(f'/api/projects/{pid}/chapters/{ch["id"]}', json=update).status_code == 200
    assert client.get(url).status_code == 409
    assert client.post(f'/api/projects/{pid}/books/{book["id"]}/release', json={
        'expected_content_sha256': book['content_sha256'], 'scope': 'family', 'signer': '陈奶奶',
        'relationship': 'self', 'evidence_note': '基于过期稿件的确认', 'confirmed': True,
    }).status_code == 409
    assert client.put(f'/api/projects/{pid}/chapters/{ch["id"]}', json=update).status_code == 409
    assert client.post(f'/api/projects/{pid}/books/{book["id"]}/order', json={'chapter_ids': [ids[0], ids[0]]}).status_code == 422


def test_editor_cannot_record_narrator_release(client):
    pid = project(client)
    book = make_book(client, pid)
    owner_cookie = client.cookies.get('studio_session')
    editor = client.post('/api/register', json={'email': 'editor-new@example.com', 'password': 'test-long-password', 'name': '编辑'}).json()
    editor_cookie = client.cookies.get('studio_session')
    client.cookies.clear()
    client.cookies.set('studio_session', owner_cookie)
    assert client.post(f'/api/projects/{pid}/members', json={'email': editor['email']}).status_code == 200
    client.cookies.clear()
    client.cookies.set('studio_session', editor_cookie)
    assert client.get(f'/api/projects/{pid}').status_code == 200
    assert acknowledge(client, pid, book['id']).status_code == 404
    assert client.delete(f'/api/projects/{pid}/books/{book["id"]}/release').status_code == 404


def test_additive_migration_preserves_data(client):
    pid = project(client)
    sid = text_source(client, pid)
    # Repeat startup is idempotent, including a pre-v2 schema upgrade.
    with db.connect() as c:
        c.execute('ALTER TABLE sources DROP COLUMN included')
        c.execute('ALTER TABLE chapters DROP COLUMN edit_revision')
    db.init()
    db.init()
    p = client.get(f'/api/projects/{pid}').json()
    assert p['sources'][0]['id'] == sid and p['sources'][0]['included'] == 1
    assert p['sources'][0]['text']


def test_empty_failed_recording_can_be_kept_without_entering_book(client):
    pid = project(client)
    result = client.post(f'/api/projects/{pid}/audio', files={'file': ('voice.wav', b'fake-audio')})
    sid = result.json()['id']
    finish_jobs(client, pid)
    assert client.put(f'/api/projects/{pid}/sources/{sid}', json={'name': '仅保留原声', 'text': '', 'included': False}).status_code == 200
    book = make_book(client, pid)
    assert len(book['chapters']) == 1
    assert client.get(f'/api/projects/{pid}/sources/{sid}/audio').status_code == 200
