"""Explicit live network test: temporarily publishes a fresh empty test server."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import hashlib
from pathlib import Path
import socket
import sys
import tempfile
import time
import uuid
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import client
client.save_folder=lambda folder:None
from network import API, parse_invite
from processes import stop
from PySide6.QtWidgets import QApplication, QDialog
app = QApplication([])
client.apply_theme(app)
folder = tempfile.mkdtemp(prefix='prism_internet_')
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
Original = client.HostDialog
class AutoDialog(Original):
    def __init__(self, parent):
        super().__init__(parent)
        self.folder.setText(folder)
        self.port.setValue(port)
    def exec(self):
        return QDialog.Accepted
client.HostDialog = AutoDialog
w = client.Window()
errors = []
w.error = lambda message: errors.append(message)
def wait(condition, seconds):
    deadline = time.monotonic() + seconds
    while not condition():
        app.processEvents()
        time.sleep(.02)
        if time.monotonic() > deadline:
            raise RuntimeError('Timeout: '+w.progress.text()+'; '+str(errors)+'; '+w.tunnel_text[-2500:])
try:
    w.create_host()
    wait(lambda: w.api is not None, 10)
    w.enable_internet()
    wait(lambda: w.tunnel_ready, 40)
    friend = parse_invite(w.friend_invite)
    assert friend.url.startswith('https://')
    cancelled=str(uuid.uuid4())
    friend.call('POST','/api/uploads',json={'name':'cancel.bin','size':4,'sha256':hashlib.sha256(b'test').hexdigest(),'transfer_id':cancelled})
    friend.call('DELETE','/api/uploads/'+cancelled)
    assert friend.call('GET','/api/chats')[0]['id']=='general'
    friend.call('POST', '/api/messages', json={'client_id':str(uuid.uuid4()), 'text':'Internet test'})
    wait(lambda: w.messages.count() == 1, 10)
    data = os.urandom(9*1024*1024+17)
    source = Path(folder) / 'internet-test.bin'
    source.write_bytes(data)
    friend.upload(source, lambda _:None)
    row = friend.call('GET','/api/messages')[-1]
    destination = Path(folder)/'received.bin'
    friend.download(row['file'],destination,lambda _:None)
    assert destination.read_bytes() == data
    assert row['sender'] == 1
    print('PASS: public HTTPS, authenticated second client, text, 9 MiB file, SHA-256',flush=True)
finally:
    w.timer.stop()
    deadline=time.monotonic()+10
    while w.jobs and time.monotonic()<deadline:
        app.processEvents();time.sleep(.02)
    if w.tunnel: stop(w.tunnel)
    if w.host: stop(w.host)
    if not w.jobs:
        w.host=None
        w.close()
