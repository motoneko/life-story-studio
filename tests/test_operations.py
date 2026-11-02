import io
import json
import zipfile
import pytest
from PIL import Image
from test_workflow import client, project, finish_jobs
from test_editorial_release import make_book, acknowledge
from app import config, db, accounts, admin


def picture():
    data=io.BytesIO(); Image.new('RGB',(300,200),(70,120,90)).save(data,format='PNG'); return data.getvalue()


def test_photo_consent_export_and_delete(client):
    pid=project(client); book=make_book(client,pid)
    response=client.post(f'/api/projects/{pid}/operations/photos',files={'file':('photo.png',picture(),'image/png')})
    assert response.status_code==200,response.text
    fid=response.json()['id'];url=f'/api/projects/{pid}/operations/photos/{fid}'
    assert client.put(url,json={'included':True,'consent':False}).status_code==422
    assert client.put(url,json={'included':True,'consent':True,'caption':'江边旧影'}).status_code==200
    assert acknowledge(client,pid,book['id']).status_code==200
    result=client.get(f'/api/projects/{pid}/export?book_id={book["id"]}&format=zip')
    assert result.status_code==200,result.text
    with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
        assert 'photos/'+fid+'.jpg' in archive.namelist()
        with zipfile.ZipFile(io.BytesIO(archive.read('book.docx'))) as doc:
            assert any(name.startswith('word/media/') for name in doc.namelist())
    assert client.delete(url).status_code==200
    assert client.get(url).status_code==404
    assert client.get(f'/api/projects/{pid}/export?book_id={book["id"]}&draft=false').status_code==409


@pytest.mark.parametrize('changed_spec', [
    {'size':'A4'}, {'binding':'精装'}, {'paper':'120g'}, {'copies':200},
    {'terms':'增加新的出版交付约定'}, {'revision_rounds':3},
])
def test_proof_approval_binds_book_and_spec(client, changed_spec):
    pid=project(client);book=make_book(client,pid);acknowledge(client,pid,book['id'])
    base=f'/api/projects/{pid}/operations'
    spec={'expected_revision':0,'book_id':book['id'],'stage':'printed'}
    assert client.put(base+'/order',json=spec).status_code==409
    spec['stage']='layout';assert client.put(base+'/order',json=spec).status_code==200
    assert client.post(base+'/proof',files={'file':('proof.pdf',b'%PDF-1.4\n%%EOF','application/pdf')}).status_code==200
    order=client.get(base).json()['order']
    assert client.post(base+'/proof/approve',json={'expected_revision':order['revision'],'proof_hash':order['proof_hash'],'signer':'本人','confirmed':True}).status_code==200
    order=client.get(base).json()['order'];old_hash=order['proof_hash']
    with db.connect() as c:
        old_path=c.execute('SELECT proof_path FROM print_orders WHERE project_id=?',(pid,)).fetchone()['proof_path']
    old_file=config.UPLOADS/'assets'/old_path
    assert old_file.exists()
    spec.update(expected_revision=order['revision'],stage='shipped',tracking='测试物流单')
    assert client.put(base+'/order',json=spec).status_code==200
    order=client.get(base).json()['order']
    # Logistics-only updates preserve the already acknowledged PDF.
    assert order['proof_hash']==old_hash and order['proof_approved_by']=='本人'
    assert old_file.exists()
    spec.update(expected_revision=order['revision'],**changed_spec)
    assert client.put(base+'/order',json=spec).status_code==409
    # Return to layout when production terms change. All proof bindings must go.
    spec['stage']='layout'
    assert client.put(base+'/order',json=spec).status_code==200
    changed=client.get(base).json()['order']
    assert changed['proof_hash'] is None and changed['proof_book_hash'] is None
    assert changed['proof_approved_by'] is None and changed['proof_approved_at'] is None
    assert not old_file.exists()
    assert client.get(base+'/proof').status_code==404
    assert client.post(base+'/proof/approve',json={'expected_revision':changed['revision'],'proof_hash':old_hash,'signer':'本人','confirmed':True}).status_code==409
    assert client.post(base+'/proof',files={'file':('new-proof.pdf',b'%PDF-1.4\n% revised production proof\n%%EOF','application/pdf')}).status_code==200
    revised=client.get(base).json()['order']
    assert revised['proof_hash']!=old_hash
    assert client.post(base+'/proof/approve',json={'expected_revision':revised['revision'],'proof_hash':revised['proof_hash'],'signer':'本人','confirmed':True}).status_code==200


def test_proof_is_removed_when_order_book_changes(client):
    pid=project(client);book=make_book(client,pid);acknowledge(client,pid,book['id'])
    base=f'/api/projects/{pid}/operations'
    assert client.put(base+'/order',json={'expected_revision':0,'book_id':book['id'],'stage':'layout'}).status_code==200
    assert client.post(base+'/proof',files={'file':('proof.pdf',b'%PDF-1.4\n%%EOF','application/pdf')}).status_code==200
    old=client.get(base).json()['order']
    with db.connect() as c:
        path=c.execute('SELECT proof_path FROM print_orders WHERE project_id=?',(pid,)).fetchone()['proof_path']
    replacement=make_book(client,pid)
    assert client.put(base+'/order',json={'expected_revision':old['revision'],'book_id':replacement['id'],'stage':'layout'}).status_code==200
    current=client.get(base).json()['order']
    assert current['proof_hash'] is None and current['proof_book_hash'] is None
    assert not (config.UPLOADS/'assets'/path).exists()
    assert client.post(base+'/proof/approve',json={'expected_revision':current['revision'],'proof_hash':old['proof_hash'],'signer':'本人','confirmed':True}).status_code==409


def test_recording_idempotency_archive_and_share_revocation(client):
    pid=project(client); url=f'/api/projects/{pid}/audio'
    first=client.post(url,files={'file':('voice.wav',b'fake-recording')},data={'recording_id':'test-recording-123'})
    assert first.status_code==200
    duplicate=client.post(url,files={'file':('voice.wav',b'fake-recording')},data={'recording_id':'test-recording-123'})
    assert duplicate.status_code==200 and duplicate.json()['id']==first.json()['id']
    finish_jobs(client,pid)
    assert len(client.get(f'/api/projects/{pid}').json()['sources'])==1
    share=client.post(f'/api/projects/{pid}/operations/shares',json={'source_id':first.json()['id'],'hours':1,'consent':True}).json()
    assert client.get(share['path']).content==b'fake-recording'
    archive=client.get(f'/api/projects/{pid}/operations/archive?include_audio=true')
    assert archive.status_code==200
    with zipfile.ZipFile(io.BytesIO(archive.content)) as z:
        assert any(name.startswith('audio/') for name in z.namelist())
        assert 'password' not in z.read('project.json').decode()
    client.delete(f'/api/projects/{pid}/operations/shares/{share["id"]}')
    assert client.get(share['path']).status_code==404


def test_password_recovery_single_use_and_session_revocation(client,monkeypatch):
    project(client);sent=[]
    monkeypatch.setenv('SMTP_HOST','test.invalid');monkeypatch.setenv('PUBLIC_BASE_URL','https://studio.example')
    monkeypatch.setattr(accounts,'send_reset',lambda email,token:sent.append((email,token)))
    assert client.post('/api/password/request',json={'email':'family@example.com'}).status_code==200
    token=sent[0][1]
    result=client.post('/api/password/reset',json={'token':token,'password':'new-long-password'})
    assert result.status_code==200,result.text
    assert client.get('/api/me').status_code==401
    assert client.post('/api/password/reset',json={'token':token,'password':'another-password'}).status_code==400
    assert client.post('/api/login',json={'email':'family@example.com','password':'new-long-password'}).status_code==200


def test_backup_restore_and_traversal(client,monkeypatch,tmp_path):
    project(client)
    monkeypatch.setattr(config,'DB',db.DB)
    # Fixture consumer owns lease; release for an explicit offline backup exercise.
    with db.connect() as c:c.execute('DELETE FROM worker_lease')
    backup=admin.backup(tmp_path/'backup.zip')
    result=admin.restore(backup,tmp_path/'restored')
    assert (result/'studio.sqlite3').exists()
    with zipfile.ZipFile(tmp_path/'bad.zip','w') as z:
        z.writestr('../escape','oops');z.writestr('studio.sqlite3',b'')
    import pytest
    with pytest.raises(RuntimeError):admin.restore(tmp_path/'bad.zip',tmp_path/'bad-restore')
