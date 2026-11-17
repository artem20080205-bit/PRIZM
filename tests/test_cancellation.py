import hashlib
from pathlib import Path
import socket
import sys
import threading
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pytest
import uvicorn
from server import configure,create_app
from network import API,TransferControl,TransferCancelled

@pytest.fixture
def live(tmp_path):
    users=configure(tmp_path/'server')['users']
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(create_app(tmp_path/'server'),host='127.0.0.1',port=port,log_level='error'))
    thread=threading.Thread(target=server.run,daemon=True);thread.start()
    deadline=time.monotonic()+5
    while not server.started:
        assert time.monotonic()<deadline
        time.sleep(.01)
    api=API(f'http://127.0.0.1:{port}',users[0]['token'],state_path=tmp_path/'state.sqlite')
    try:yield api,tmp_path
    finally:server.should_exit=True;thread.join(5)

def source(root):
    path=root/'source.bin'
    path.write_bytes(b'12345678'*(1024*1024))
    return path

def test_upload_cancel_removes_remote_partial_and_preserves_source(live):
    api,root=live;path=source(root);control=TransferControl()
    with pytest.raises(TransferCancelled):
        api.upload(path,lambda text:control.cancel() if text.startswith('Отправка:') else None,control)
    assert path.stat().st_size==8*1024*1024
    assert list((root/'server'/'files').iterdir())==[]
    assert api.state.pending(api.profile)==[]
    assert api.call('GET','/api/messages')==[]

def test_download_cancel_removes_sidecar_not_existing_destination(live):
    api,root=live;path=source(root)
    api.upload(path,lambda text:None)
    attachment=api.call('GET','/api/messages')[-1]['file']
    destination=root/'destination.bin';destination.write_bytes(b'previous file')
    control=TransferControl()
    with pytest.raises(TransferCancelled):
        api.download(attachment,destination,lambda text:control.cancel() if text.startswith('Скачивание:') else None,control)
    assert destination.read_bytes()==b'previous file'
    assert list(root.glob('*.part'))==[]
    assert (root/'server'/'files'/attachment['id']).exists()

def test_offline_cancellation_is_durable_and_retried(live):
    api,root=live;path=source(root);control=TransferControl();original=api.call
    def offline_delete(method,url,**kwargs):
        if method=='DELETE':raise OSError('offline')
        return original(method,url,**kwargs)
    api.call=offline_delete
    with pytest.raises(TransferCancelled):
        api.upload(path,lambda text:control.cancel() if text.startswith('Отправка:') else None,control)
    assert len(api.state.pending(api.profile))==1
    assert len(list((root/'server'/'files').iterdir()))==1
    # A newly created client processes the persistent queue after reconnection.
    reconnected=API(api.url,api.token,state_path=root/'state.sqlite')
    assert reconnected.flush_cancellations()==0
    assert list((root/'server'/'files').iterdir())==[]

def test_cancel_at_finalization_and_before_start(live):
    api,root=live;path=source(root);control=TransferControl()
    with pytest.raises(TransferCancelled):
        api.upload(path,lambda text:control.cancel() if text.startswith('Сервер проверяет') else None,control)
    assert list((root/'server'/'files').iterdir())==[]
    assert api.call('GET','/api/messages')==[]
    cancelled=TransferControl();cancelled.cancel()
    with pytest.raises(TransferCancelled):api.upload(path,lambda text:None,cancelled)
    assert list((root/'server'/'files').iterdir())==[]
