"""Standalone browser regression: virtual microphone and mocked server responses.
Run: python tests/browser_recording_recovery.py
Requires Playwright + Chromium; no live AI keys, user audio, or running app needed.
This validates desktop Chromium recovery, not physical mobile devices or OS crashes.
"""
import json
import threading
import time
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, expect

STATIC = Path(__file__).resolve().parents[1] / 'app' / 'static'


class StaticHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def do_GET(self):
        if self.path == '/':
            html = (STATIC / 'index.html').read_text()
            # Exercise the frontend modules under test without depending on optional extensions.
            import re
            html = re.sub(r'<script\b[^>]*>.*?</script>', '', html)
            html = html.replace('</head>', '<script src="/static/recording-store.js" defer></script><script src="/static/app.js" defer></script></head>')
            encoded = html.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        if self.path.startswith('/static/'):
            self.path = self.path[len('/static'):]
        super().do_GET()

    def log_message(self, *_):
        pass


def wait_for_chunks(page, minimum):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        rows = page.evaluate("async () => await RecordingStore.list('user-a','project-a')")
        if rows and rows[0]['chunks'] >= minimum and rows[0]['bytes'] > 0:
            return
        page.wait_for_timeout(100)
    raise AssertionError('No persisted audio chunks before timeout')


def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), StaticHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    current = {'user': 'a', 'fail_upload': True, 'uploads': 0}
    projects = {}
    for key in ('a', 'b'):
        projects[key] = {'id': f'project-{key}', 'owner_id': f'user-{key}', 'title': f'测试故事{key}', 'name': '测试讲述者',
                         'style': '质朴亲切', 'created_at': '2026-11-02T00:00:00Z', 'revision': 0,
                         'source_count': 0, 'book_count': 0, 'members': [], 'audit': [], 'jobs': [], 'books': [], 'sources': [],
                         'messages': [{'role': 'assistant', 'content': '能聊聊小时候最喜欢的地方吗？'}]}

    def route_api(route):
        request = route.request
        path = urlsplit(request.url).path[len('/api'):]
        key = current['user']
        project = projects[key]
        if path == '/config':
            result = {'mode': 'demo', 'max_upload_mb': 200, 'questions': [['童年', ['小时候在哪里？']]]}
        elif path == '/me':
            result = {'id': f'user-{key}', 'name': f'测试账号{key}', 'email': f'{key}@example.test'}
        elif path == '/projects':
            result = [project]
        elif path == f'/projects/project-{key}':
            result = project
        elif path == f'/projects/project-{key}/audio':
            current['uploads'] += 1
            if current['fail_upload']:
                route.fulfill(status=503, content_type='application/json', body=json.dumps({'detail': '测试：上传中断'}))
                return
            assert b'recording_id' in request.post_data_buffer
            result = {'id': 'source-uploaded', 'job_id': 'job-uploaded'}
        elif path == '/password/request':
            assert request.post_data_json['email'] == 'a@example.test'
            result = {'message': '如邮箱已注册，将收到重设链接'}
        elif path == '/password/reset':
            assert request.post_data_json['token'] == 'test-reset-token'
            assert request.post_data_json['password'] == 'new-example-password'
            result = {'ok': True}
        elif path == '/logout':
            result = {'ok': True}
        else:
            route.fulfill(status=404, content_type='application/json', body=json.dumps({'detail': '测试路由不存在'}))
            return
        route.fulfill(content_type='application/json', body=json.dumps(result, ensure_ascii=False))

    try:
        with sync_playwright() as play:
            browser = play.chromium.launch(headless=True, args=['--no-sandbox', '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'])
            page = browser.new_page(viewport={'width': 1280, 'height': 900})
            errors = []
            dialogs = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('dialog', lambda dialog: (dialogs.append(dialog.message), dialog.accept()))
            page.route('**/api/**', route_api)
            page.goto(f'http://127.0.0.1:{server.server_port}')
            page.get_by_role('button', name='专心讲述', exact=True).click()
            page.locator('[data-project=project-a]').click()
            expect(page.locator('.sidebar')).not_to_be_visible()
            expect(page.locator('.focused-question')).to_contain_text('小时候')
            page.get_by_role('button', name='● 开始讲述', exact=True).click()
            expect(page.get_by_role('button', name='■ 结束并试听', exact=True)).to_be_visible()
            wait_for_chunks(page, 2)
            # Refresh during active recording. Only committed chunks are guaranteed to survive.
            page.reload()
            expect(page.locator('[data-local-recording]')).to_have_count(1)
            page.locator('[data-project=project-a]').click()
            expect(page.locator('[data-local-recording]')).to_have_count(1)
            page.get_by_role('button', name='恢复并试听', exact=True).click()
            expect(page.locator('.local-recording audio')).to_be_visible()
            page.get_by_role('button', name='确认并上传', exact=True).click()
            expect(page.locator('.local-recording')).to_contain_text('上传未确认成功')
            assert len(page.evaluate("async () => await RecordingStore.list('user-a','project-a')")) == 1
            with page.expect_download() as download:
                page.get_by_role('button', name='下载备份', exact=True).click()
            assert download.value.suggested_filename.endswith('.webm')
            # A second login must not render the first account's recovery content.
            current['user'] = 'b'
            page.reload()
            expect(page.locator('[data-project=project-b]')).to_be_visible()
            expect(page.locator('[data-local-recording]')).to_have_count(0)
            assert page.evaluate("async () => { const row=(await RecordingStore.list('user-a'))[0]; try { await RecordingStore.read('user-b','project-a',row.id); return false; } catch { return true; } }")
            # Restore first account and retry succeeds; local audio must be deleted after acknowledgement.
            current['user'] = 'a'
            current['fail_upload'] = False
            page.reload()
            page.locator('[data-project=project-a]').click()
            page.get_by_role('button', name='恢复并试听', exact=True).click()
            page.get_by_role('button', name='确认并上传', exact=True).click()
            expect(page.locator('[data-local-recording]')).to_have_count(0)
            assert page.evaluate("async () => (await RecordingStore.list('user-a')).length") == 0
            assert current['uploads'] == 2
            # Stop by a microphone ended event, preserving whatever MediaRecorder delivered.
            page.get_by_role('button', name='● 开始讲述', exact=True).click()
            wait_for_chunks(page, 1)
            page.evaluate("stream.getTracks()[0].dispatchEvent(new Event('ended'))")
            expect(page.locator('[data-local-recording]')).to_have_count(1)
            expect(page.locator('.local-recording')).to_contain_text('麦克风连接已结束')
            page.get_by_role('button', name='完整工作台', exact=True).click()
            # Late extension registration is rendered and receives click/submit dispatch.
            page.evaluate("""window.StudioExtensions.push({views:{editorial:()=>'<button data-extension-test="yes">扩展测试按钮</button>'},click:async target=>{if(target.dataset.extensionTest){target.textContent='扩展已响应';return true;}return false;}})""")
            page.locator('[data-view=editorial]').click()
            page.get_by_role('button', name='扩展测试按钮').click()
            expect(page.get_by_role('button', name='扩展已响应')).to_be_visible()
            # Logout explicitly discloses pending audio deletion; clear only this user's records.
            page.evaluate("async () => { const r=await RecordingStore.create('user-b','project-b','audio/webm'); await RecordingStore.append('user-b','project-b',r.id,0,new Blob(['other account'])); }")
            page.get_by_title('退出登录').click()
            expect(page.locator('#auth-form')).to_be_visible()
            assert any('未上传且未备份的内容无法恢复' in message for message in dialogs)
            assert page.evaluate("async () => (await RecordingStore.list('user-a')).length") == 0
            assert page.evaluate("async () => (await RecordingStore.list('user-b')).length") == 1
            page.get_by_role('button', name='忘记密码', exact=True).click()
            page.get_by_label('注册邮箱', exact=True).fill('a@example.test')
            page.get_by_role('button', name='发送重设链接', exact=True).click()
            expect(page.locator('#modal')).not_to_be_visible()
            page.goto(f'http://127.0.0.1:{server.server_port}/#reset=test-reset-token')
            expect(page.locator('#password-reset-form')).to_be_visible()
            assert page.evaluate('location.hash') == ''
            page.get_by_label('新密码', exact=True).fill('new-example-password')
            page.get_by_role('button', name='更新密码', exact=True).click()
            expect(page.locator('#auth-form')).to_be_visible()
            assert page.evaluate("Object.values(localStorage).every(value => !value.includes('test-reset-token'))")
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
    print('PASS: desktop Chromium recording chunks, refresh recovery, preview, retry/download, user/project isolation, track-ended preservation, explicit logout cleanup extension registration and password recovery UI. Physical mobile/OS-crash behavior not tested.')


if __name__ == '__main__':
    main()
