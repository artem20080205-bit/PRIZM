import os
os.environ['QT_QPA_PLATFORM']='offscreen'
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import uuid
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import uvicorn
from PySide6.QtWidgets import QApplication,QGroupBox
from client import Window,apply_theme
from network import API
from server import configure,create_app
from settings_ui import StorageDialog
app=QApplication([]);apply_theme(app)
with tempfile.TemporaryDirectory() as temporary:
    folder=Path(temporary)
    users=configure(folder/'server')['users']
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(create_app(folder/'server'),host='127.0.0.1',port=port,log_level='error'))
    thread=threading.Thread(target=server.run,daemon=True);thread.start()
    while not server.started:time.sleep(.01)
    apis=[API(f'http://127.0.0.1:{port}',u['token'],state_path=folder/f'client{i}.sqlite') for i,u in enumerate(users)]
    windows=[Window(),Window()]
    def wait(condition):
        deadline=time.monotonic()+15
        while not condition():
            app.processEvents();time.sleep(.005)
            assert time.monotonic()<deadline,'UI timeout'
    try:
        for window,api in zip(windows,apis):window.show();window.connect_api(api)
        wait(lambda:all(w.api is not None for w in windows))
        second=apis[0].call('POST','/api/chats',json={'title':'Проект'})['id']
        wait(lambda:all(w.chat_select.count()==2 for w in windows))
        for w in windows:w.chat_select.setCurrentIndex(w.chat_select.findData(second))
        windows[0].text.setText('Второй чат');windows[0].send()
        wait(lambda:all(w.messages.count()==1 for w in windows))
        data=apis[0].call('GET','/api/storage')
        dialog=StorageDialog(windows[0],data);dialog.show();app.processEvents()
        assert len(dialog.findChildren(QGroupBox))==3
        assert len(dialog.actions)==9
        assert len(StorageDialog(windows[1]).actions)==0
        dialog.grab().save(str(ROOT/'settings_preview.png'))
        dialog.close()
        # Only the chosen chat is cleared and both clients refresh its revision.
        apis[0].call('POST','/api/storage/clear',json={'mode':'all','chat_id':second})
        wait(lambda:all(w.messages.count()==0 for w in windows))
        windows[0].text.setText('После очистки');windows[0].send()
        wait(lambda:all(w.messages.count()==1 for w in windows))
        source=folder/'upload.bin';source.write_bytes(b'0'*(12*1024*1024))
        def slow_upload(report,control):
            def progress(text):report(text);time.sleep(.08)
            return apis[0].upload(source,progress,control)
        windows[0].transfer_job(slow_upload)
        wait(lambda:windows[0].progress.text().startswith('Отправка:'))
        assert windows[0].cancel_button.isVisible()
        windows[0].cancel_button.click()
        wait(lambda:not windows[0].transfer)
        assert 'Отменено' in windows[0].progress.text()
        assert list((folder/'server'/'files').iterdir())==[]
        assert source.exists()
        print('PASS: chat selector, host settings, per-chat refresh, real Cancel button and remote deletion')
    finally:
        for w in windows:w.timer.stop()
        wait(lambda:all(not w.jobs for w in windows))
        for w in windows:w.close()
        server.should_exit=True;thread.join(5)
