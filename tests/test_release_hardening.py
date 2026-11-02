import ssl
import pytest
from app import accounts, admin, config


def test_password_email_verifies_server_certificate(monkeypatch):
    calls=[]

    class SMTP:
        def __init__(self, host, port, timeout):
            assert (host, port, timeout)==('smtp.example', 587, 20)
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def starttls(self, *, context):
            assert context.verify_mode==ssl.CERT_REQUIRED
            assert context.check_hostname
            calls.append('verified-tls')
        def login(self, user, password):
            assert calls==['verified-tls']
            calls.append('login')
        def send_message(self, message):
            assert calls==['verified-tls','login']
            assert message['To']=='family@example.com'
            assert 'https://studio.example/#reset=test-token' in message.get_content()
            calls.append('sent')

    monkeypatch.setenv('SMTP_HOST','smtp.example')
    monkeypatch.setenv('SMTP_PORT','587')
    monkeypatch.setenv('SMTP_USER','studio@example.com')
    monkeypatch.setenv('SMTP_PASSWORD','test-mail-password')
    monkeypatch.setenv('PUBLIC_BASE_URL','https://studio.example')
    monkeypatch.setattr(accounts.smtplib,'SMTP',SMTP)
    accounts.send_reset('family@example.com','test-token')
    assert calls[-1]=='sent'


def test_backup_rejects_self_inclusion_and_symlinks(monkeypatch,tmp_path):
    uploads=tmp_path/'uploads';uploads.mkdir()
    monkeypatch.setattr(config,'UPLOADS',uploads)
    with pytest.raises(RuntimeError,match='素材目录'):
        admin.backup(uploads/'backup.zip')
    assert not (uploads/'backup.zip').exists()
    secret=tmp_path/'private.txt';secret.write_text('outside uploads')
    (uploads/'link').symlink_to(secret)
    destination=tmp_path/'backup.zip'
    with pytest.raises(RuntimeError,match='符号链接'):
        admin.backup(destination)
    assert not destination.exists()
