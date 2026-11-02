"""Real FFmpeg + real local HTTP transport, fake ASR response (no recognition claim)."""
import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from app import ai, config


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='requires system ffmpeg and ffprobe')
def test_real_decode_and_multipart_with_local_fake_asr(tmp_path, monkeypatch):
    source = tmp_path / 'silence.wav'
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo', '-t', '1.25', str(source)], check=True, timeout=15)
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            requests.append((self.path, self.headers, body))
            result = json.dumps({'text': '模拟 ASR 返回，用于验证传输。', 'segments': [{'start': 0, 'end': 1.0, 'text': '模拟 ASR 返回，用于验证传输。', 'speaker': '0'}]}, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(result)))
            self.end_headers()
            self.wfile.write(result)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(config, 'AI_MODE', 'live')
    monkeypatch.setenv('ASR_BASE_URL', f'http://127.0.0.1:{server.server_port}/v1')
    monkeypatch.setenv('ASR_API_KEY', 'local-test-only')
    monkeypatch.setenv('ASR_MODEL', 'fake-local-asr')
    monkeypatch.setenv('ASR_RESPONSE_FORMAT', 'verbose_json')
    try:
        text, segments = ai.transcribe(source, lambda _: None)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    assert text == '模拟 ASR 返回，用于验证传输。'
    assert len(requests) == 1
    path, headers, body = requests[0]
    assert path == '/v1/audio/transcriptions'
    assert 'multipart/form-data' in headers['Content-Type']
    assert b'RIFF' in body and b'WAVE' in body
    assert b'fake-local-asr' in body
    assert segments[0]['precision'] == 'provider_segment'
    assert segments[0]['speaker'] == '0'
