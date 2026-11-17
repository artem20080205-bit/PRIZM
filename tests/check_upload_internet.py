"""Opt-in external test, fresh disposable storage, never real chat data."""
from pathlib import Path
import hashlib
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn
from internet import prepare
from network import API
from server import configure, create_app

root = Path(tempfile.mkdtemp(prefix='prism_upload_'))
users = configure(root/'server')['users']
with socket.socket() as sock:
    sock.bind(('127.0.0.1',0))
    port = sock.getsockname()[1]
server = uvicorn.Server(uvicorn.Config(create_app(root/'server'), host='127.0.0.1', port=port, log_level='error'))
thread = threading.Thread(target=server.run, daemon=True)
thread.start()
tunnel = None
try:
    with (root/'tunnel.log').open('w') as log:
        tunnel = subprocess.Popen([prepare(), 'tunnel','--no-autoupdate','--protocol','http2','--url',f'http://127.0.0.1:{port}'], stdout=log, stderr=log, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        deadline = time.monotonic()+50
        while time.monotonic()<deadline:
            match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', (root/'tunnel.log').read_text())
            if match:break
            time.sleep(.2)
        else:raise RuntimeError('Tunnel address unavailable; log: '+str(root/'tunnel.log'))
        friend = API(match.group(), users[1]['token'], state_path=root/'friend.sqlite')
        for attempt in range(15):
            try:
                assert friend.call('GET','/api/me')['id']==1
                break
            except Exception:
                if attempt==14:raise
                time.sleep(1)
        source = root/'sample.bin'
        source.write_bytes(os.urandom(9*1024*1024+17))
        friend.upload(source, lambda _:None)
        host = API(f'http://127.0.0.1:{port}', users[0]['token'], state_path=root/'host.sqlite')
        rows = host.call('GET','/api/messages')
        assert len(rows)==1 and rows[0]['sender']==1
        attachment=rows[0]['file']
        assert hashlib.sha256((root/'server'/'files'/attachment['id']).read_bytes()).hexdigest()==hashlib.sha256(source.read_bytes()).hexdigest()
        print('PASS: friend uploaded 9 MiB over public HTTPS; host received identical SHA-256',flush=True)
finally:
    if tunnel:
        tunnel.terminate()
        try:tunnel.wait(5)
        except subprocess.TimeoutExpired:tunnel.kill();tunnel.wait()
    server.should_exit=True
    thread.join(5)
