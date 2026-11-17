import os, sys, time, tempfile, subprocess, hashlib, uuid, socket
import psutil
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import configure
from network import API
from client import Window, apply_theme
from PySide6.QtWidgets import QApplication
os.environ['QT_QPA_PLATFORM']='offscreen'
app=QApplication([])
apply_theme(app)
with tempfile.TemporaryDirectory() as folder:
 users=configure(folder)['users']
 with socket.socket() as sock:
  sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
 proc=subprocess.Popen([sys.executable, str(ROOT / 'server.py'),'--data',folder,'--port',str(port)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 try:
  apis=[API(f'http://127.0.0.1:{port}',u['token']) for u in users]
  for _ in range(80):
   try: apis[0].call('GET','/api/me');break
   except Exception: time.sleep(.05)
  windows=[Window(),Window()]
  def wait_until(condition):
   end=time.monotonic()+15
   while not condition():
    app.processEvents();time.sleep(.01)
    if time.monotonic()>end: raise AssertionError('Timeout')
  for w,api in zip(windows,apis): w.show();w.connect_api(api)
  wait_until(lambda:all(w.api for w in windows))
  windows[0].text.setText('Привет! Проверяем первый рабочий чат.');windows[0].send()
  wait_until(lambda:all(w.messages.count()==1 for w in windows))
  assert 'Привет!' in windows[1].messages.item(0).text()
  windows[1].text.setText('Сообщение дошло. Теперь отправляю файл.');windows[1].send()
  wait_until(lambda:all(w.messages.count()==2 for w in windows))
  source=Path(folder)/'sample.bin';source.write_bytes(os.urandom(9*1024*1024+17))
  # Seed an interrupted upload, then resume through the real network client.
  data=source.read_bytes();sha=hashlib.sha256(data).hexdigest()
  record=apis[0].call('POST','/api/uploads',json={'name':source.name,'size':len(data),'sha256':sha})
  with apis[0].client() as c:
   c.put('/api/uploads/'+record['id'],params={'offset':0},content=data[:4*1024*1024]).raise_for_status()
  apis[0].upload(source,lambda _:None)
  wait_until(lambda:all(w.messages.count()==3 for w in windows))
  attachment=windows[1].messages.item(2).data(__import__('PySide6.QtCore',fromlist=['Qt']).Qt.UserRole)
  dest=Path(folder)/'download.bin'
  dest.with_name(dest.name+'.'+attachment['id']+'.part').write_bytes(data[:1024*1024])
  apis[1].download(attachment,dest,lambda _:None)
  assert hashlib.sha256(dest.read_bytes()).hexdigest()==sha
  # Rendering is also exercised in offscreen mode.
  for w in windows:w.timer.stop()
  wait_until(lambda:all(not w.jobs for w in windows))
  for w in windows:w.close()
  print('PASS: two Qt windows, bidirectional messages, 9 MiB upload resume, download resume, SHA-256')
 finally:
  children=psutil.Process(proc.pid).children(recursive=True)
  for child in children:
   try:child.terminate()
   except psutil.NoSuchProcess:pass
  proc.terminate();proc.wait(timeout=5)
  psutil.wait_procs(children,timeout=5)
