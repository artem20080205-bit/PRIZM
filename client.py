"""Desktop UI. Run: python client.py"""
import datetime
import os
import re
import time
from pathlib import Path
import sys
import uuid

from PySide6.QtCore import QIODevice, QProcess, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QFontDatabase, QFont
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget, QComboBox, QInputDialog)
from network import API, invite, parse_invite, TransferControl, TransferCancelled
from server import configure
from internet import prepare
from processes import stop
from settings_ui import StorageDialog, preferred_folder, save_folder


class Job(QThread):
    result = Signal(object)
    error = Signal(str)
    progress = Signal(str)
    cancelled = Signal(str)
    def __init__(self, task, parent):
        super().__init__(parent)
        self.task = task
    def run(self):
        try:
            self.result.emit(self.task(self.progress.emit))
        except TransferCancelled as e:
            self.cancelled.emit(str(e))
        except Exception as e:
            self.error.emit(str(e))


class HostDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle('Создать свой сервер')
        self.resize(520, 200)
        layout = QFormLayout(self)
        default = Path(preferred_folder())
        self.folder = QLineEdit(str(default))
        self.port = QSpinBox(); self.port.setRange(1024, 65535); self.port.setValue(8765)
        self.address = QLineEdit('http://127.0.0.1:8765')
        folder_row=QHBoxLayout();folder_row.addWidget(self.folder)
        browse=QPushButton('Выбрать…')
        def choose_folder():
            selected=QFileDialog.getExistingDirectory(self,'Папка сервера',self.folder.text())
            if selected:self.folder.setText(selected)
        browse.clicked.connect(choose_folder);folder_row.addWidget(browse)
        layout.addRow('Папка данных', folder_row)
        layout.addRow('Порт', self.port)
        self.address.hide()
        label = QLabel('После запуска нажмите «Включить интернет».\nАдрес и приглашение будут созданы автоматически.\nСервер работает, пока это окно открыто.\nПрототип: сквозного шифрования пока нет.')
        label.setWordWrap(True); layout.addRow(label)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        layout.addRow(buttons)


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('PRISM · интернет-чат · прототип 0.3.1')
        self.resize(850, 660)
        self.display_name = ''; self.api = None; self.cursor = 0; self.jobs = set()
        self.polling = False; self.transfer = False; self.sending = False
        self.pending = None; self.host = None; self.server_log = None
        self.is_host=False; self.chat_revision=None; self.transfer_control=None
        self.tunnel = None; self.tunnel_starting = False; self.tunnel_ready = False; self.tunnel_text = ""
        self.timer = QTimer(self); self.timer.setInterval(900); self.timer.timeout.connect(self.poll)
        root = QWidget(); self.setCentralWidget(root); layout = QVBoxLayout(root)
        title = QLabel('PRISM'); title.setObjectName('title'); layout.addWidget(title)
        banner = QLabel('Тестовый чат • E2EE и ИИ ещё нет • интернет-режим передаёт данные через Cloudflare')
        banner.setObjectName('warning'); banner.setWordWrap(True); layout.addWidget(banner)
        bar = QHBoxLayout()
        self.host_button = QPushButton('Создать сервер'); self.host_button.clicked.connect(self.create_host)
        self.join_button = QPushButton('Подключиться'); self.join_button.clicked.connect(self.join)
        self.leave_button = QPushButton('Отключиться'); self.leave_button.clicked.connect(self.leave)
        self.invite_button = QPushButton('Приглашение другу'); self.invite_button.setEnabled(False)
        self.invite_button.clicked.connect(self.copy_invite)
        self.internet_button = QPushButton('Включить интернет'); self.internet_button.clicked.connect(self.enable_internet)
        for button in [self.host_button, self.join_button, self.leave_button, self.internet_button, self.invite_button]: bar.addWidget(button)
        layout.addLayout(bar)
        chatbar=QHBoxLayout()
        self.chat_select=QComboBox();self.chat_select.addItem('Общий чат','general')
        self.chat_select.currentIndexChanged.connect(self.select_chat)
        self.new_chat_button=QPushButton('Новый чат');self.new_chat_button.clicked.connect(self.new_chat)
        self.settings_button=QPushButton('Настройки');self.settings_button.clicked.connect(self.settings)
        chatbar.addWidget(self.chat_select,1);chatbar.addWidget(self.new_chat_button);chatbar.addWidget(self.settings_button)
        layout.addLayout(chatbar)
        self.status = QLabel('Создайте сервер или подключитесь по приглашению'); layout.addWidget(self.status)
        self.messages = QListWidget(); self.messages.setWordWrap(True)
        self.messages.itemDoubleClicked.connect(self.download); layout.addWidget(self.messages, 1)
        hint = QLabel('Файл в переписке: двойной щелчок для сохранения.'); layout.addWidget(hint)
        composer = QHBoxLayout(); self.text = QLineEdit(); self.text.setPlaceholderText('Сообщение…')
        self.text.setMaxLength(16000); self.text.returnPressed.connect(self.send)
        self.send_button = QPushButton('Отправить'); self.send_button.clicked.connect(self.send)
        self.file_button = QPushButton('Прикрепить файл'); self.file_button.clicked.connect(self.upload)
        composer.addWidget(self.text, 1); composer.addWidget(self.file_button); composer.addWidget(self.send_button)
        layout.addLayout(composer)
        self.cancel_button=QPushButton('Отменить передачу');self.cancel_button.clicked.connect(self.cancel_transfer)
        self.cancel_button.setVisible(False);layout.addWidget(self.cancel_button)
        self.progress = QLabel(''); self.progress.setWordWrap(True); layout.addWidget(self.progress)
        self.controls()

    def controls(self):
        connected = self.api is not None
        self.settings_button.setEnabled(not self.transfer and not self.sending)
        self.chat_select.setEnabled(connected and not self.transfer and not self.sending)
        self.new_chat_button.setEnabled(connected and self.is_host and not self.transfer and not self.sending)
        self.cancel_button.setVisible(self.transfer)
        self.internet_button.setEnabled(self.host is not None and connected and not self.tunnel_starting and not self.tunnel_ready)
        self.host_button.setEnabled(not connected and self.host is None and not self.jobs)
        self.send_button.setEnabled(connected and not self.sending)
        self.file_button.setEnabled(connected and not self.transfer)
        self.leave_button.setEnabled(connected and not self.transfer and not self.sending)
        self.join_button.setEnabled(not connected and not self.jobs)
        self.text.setEnabled(connected and not self.sending)

    def job(self, task, success, error=None, cancelled=None):
        job = Job(task, self); self.jobs.add(job)
        job.result.connect(success, Qt.QueuedConnection)
        job.error.connect(error or self.error, Qt.QueuedConnection)
        job.cancelled.connect(cancelled or self.progress.setText,Qt.QueuedConnection)
        job.progress.connect(self.progress.setText, Qt.QueuedConnection)
        def cleanup():
            self.jobs.discard(job); job.deleteLater(); self.controls()
        job.finished.connect(cleanup)
        job.start(); self.controls()

    def error(self, message):
        QMessageBox.warning(self, 'Не удалось выполнить действие', message)

    def connect_api(self, api):
        def ready(me):
            if me.get('protocol',0)<3:
                self.error('Обновите сервер и оба клиента до PRISM 0.3.');return
            self.is_host=me.get('is_host',False);self.chat_revision=None
            self.display_name = me["name"]; self.api = api; self.cursor = 0; self.messages.clear(); self.pending = None
            self.status.setText(f'{me["name"]} · {api.url} · общий чат на двух участников')
            self.controls(); self.timer.start(); self.poll()
        self.job(lambda _: api.call('GET', '/api/me'), ready)

    def join(self):
        dialog = QDialog(self); dialog.setWindowTitle('Подключиться'); dialog.resize(580, 140)
        form = QFormLayout(dialog); entry = QLineEdit(); entry.setEchoMode(QLineEdit.Password)
        form.addRow('Вставьте приглашение', entry)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject); form.addRow(buttons)
        if dialog.exec() == QDialog.Accepted:
            try: self.connect_api(parse_invite(entry.text()))
            except Exception as e: self.error(str(e))

    def create_host(self):
        if self.host is not None:
            self.error('Сервер уже запущен в этом окне.'); return
        dialog = HostDialog(self)
        if dialog.exec() != QDialog.Accepted: return
        try:
            config = configure(dialog.folder.text())
            save_folder(dialog.folder.text())
            port = dialog.port.value()
            self.friend_invite = invite(dialog.address.text(), config['users'][1]['token'])
            parse_invite(self.friend_invite)
            local = API(f'http://127.0.0.1:{port}', config['users'][0]['token'])
            # Fail early rather than accidentally connecting to another process.
            import socket
            with socket.socket() as sock: sock.bind(('0.0.0.0', port))
            self.host = QProcess(self)
            self.server_log = str(Path(dialog.folder.text()) / 'server.log')
            self.host.setStandardOutputFile(self.server_log)
            self.host.setStandardErrorFile(self.server_log, QIODevice.Append)
            self.host.setProgram(sys.executable)
            self.host.setArguments([str(Path(__file__).with_name('server.py')), '--data',
                                    str(Path(dialog.folder.text()).resolve()), '--host', '127.0.0.1', '--port', str(port)])
            self.host.start()
            self.host_button.setEnabled(False)
            self.progress.setText('Запускается сервер…')
            self.start_attempt = 0
            def attempt():
                self.start_attempt += 1
                try:
                    local.call('GET', '/api/me')
                except Exception:
                    if self.start_attempt < 15 and self.host.state() != QProcess.NotRunning:
                        QTimer.singleShot(300, attempt); return
                    self.error('Сервер не запустился. Смотрите server.log в папке данных.')
                    self.host.kill(); self.host.waitForFinished(2000); self.host = None
                    self.host_button.setEnabled(True); return
                self.host_token2 = config['users'][1]['token']
                self.host_port = port
                self.invite_button.setEnabled(False); self.progress.setText('Сервер запущен. Нажмите «Включить интернет», чтобы подключить друга.')
                self.connect_api(local)
            QTimer.singleShot(500, attempt)
        except Exception as e: self.error(str(e))


    def enable_internet(self):
        if not self.host or self.tunnel_starting or self.tunnel_ready:
            return
        self.tunnel_starting = True
        self.controls()
        def failed(message):
            self.tunnel_starting = False
            self.controls()
            self.error('Не удалось включить интернет. '+message)
        def launch(executable):
            self.tunnel = QProcess(self)
            self.tunnel.setProcessChannelMode(QProcess.MergedChannels)
            self.tunnel_text = ''
            self.tunnel_url = None
            self.tunnel_deadline = time.monotonic() + 80
            def read_logs():
                self.tunnel_text = (self.tunnel_text + bytes(self.tunnel.readAllStandardOutput()).decode('utf8', 'replace'))[-20000:]
                match = re.search(r'https://[a-z0-9-]+[.]trycloudflare[.]com', self.tunnel_text)
                if match and not self.tunnel_url:
                    self.tunnel_url = match.group(0)
                    self.check_public()
            self.tunnel.readyReadStandardOutput.connect(read_logs)
            def ended(*args):
                was_ready = self.tunnel_ready
                self.tunnel_ready = False
                self.tunnel_starting = False
                self.invite_button.setEnabled(False)
                self.controls()
                if was_ready:
                    self.progress.setText('Интернет-канал отключён. Включите заново и передайте новое приглашение.')
                else:
                    self.progress.setText('Интернет-канал не запустился. Смотрите tunnel.log в папке проекта.')
                try:
                    Path(__file__).with_name('tunnel.log').write_text(self.tunnel_text, encoding='utf8')
                except OSError:
                    pass
            self.tunnel.finished.connect(ended)
            self.tunnel.errorOccurred.connect(lambda error: failed('Ошибка запуска cloudflared: '+self.tunnel.errorString()))
            self.tunnel.setProgram(executable)
            self.tunnel.setArguments(['tunnel', '--no-autoupdate', '--protocol', 'http2', '--url', f'http://127.0.0.1:{self.host_port}'])
            self.tunnel.start()
            self.progress.setText('Создаётся интернет-адрес…')
            def expired():
                if self.tunnel_starting:
                    self.tunnel_starting = False
                    self.progress.setText('Не удалось открыть интернет-канал. Доступность Cloudflare зависит от сети.')
                    self.tunnel.kill()
                    self.controls()
            QTimer.singleShot(85000, expired)
        self.job(lambda report: prepare(report), launch, failed)

    def check_public(self):
        if not self.tunnel_starting or not self.tunnel_url:
            return
        public = API(self.tunnel_url, self.host_token2)
        def ready(me):
            self.tunnel_starting = False
            self.tunnel_ready = True
            self.friend_invite = invite(self.tunnel_url, self.host_token2)
            self.invite_button.setEnabled(True)
            self.progress.setText('Интернет работает. Нажмите «Приглашение другу» и отправьте новое приглашение.')
            self.internet_button.setText('Интернет включён')
            self.controls()
        def retry(error):
            if self.tunnel_starting and time.monotonic() < self.tunnel_deadline:
                QTimer.singleShot(1500, self.check_public)
        self.job(lambda _: public.call('GET', '/api/me', timeout=8), ready, retry)

    def copy_invite(self):
        QApplication.clipboard().setText(self.friend_invite)
        self.progress.setText('Приглашение скопировано. Оно даёт доступ к чату — передавайте только другу.')

    def leave(self):
        if self.polling:
            self.progress.setText('Дождитесь окончания проверки соединения и повторите.'); return
        self.timer.stop(); self.api = None; self.messages.clear(); self.cursor = 0
        self.status.setText('Отключено'); self.controls()

    def poll(self):
        if not self.api or self.polling:return
        self.polling=True
        api=self.api;key=api.chat_id;cursor=self.cursor;revision=self.chat_revision
        def fetch(_):
            pending=api.flush_cancellations()
            chats=api.call('GET','/api/chats',timeout=5)
            selected=next((chat for chat in chats if chat['id']==key),None)
            if not selected:raise RuntimeError('Чат не найден')
            changed=selected['revision']!=revision
            rows=api.call('GET','/api/messages',params={'after':0 if changed else cursor,'chat_id':key},timeout=5)
            return {'chats':chats,'revision':selected['revision'],'reset':changed,'messages':rows,'pending':pending}
        def received(data):
            self.polling=False
            if self.api is not api or api.chat_id!=key:return
            self.chat_select.blockSignals(True)
            self.chat_select.clear()
            for chat in data['chats']:self.chat_select.addItem(chat['title'],chat['id'])
            self.chat_select.setCurrentIndex(max(0,self.chat_select.findData(key)))
            self.chat_select.blockSignals(False)
            if data['reset']:self.messages.clear();self.cursor=0
            self.chat_revision=data['revision']
            for row in data['messages']:
                self.cursor=max(self.cursor,row['id'])
                stamp=datetime.datetime.fromtimestamp(row['created']).strftime('%H:%M')
                body=row['text']
                if row.get('file'):
                    attachment=row['file'];body=f'📎 {attachment["name"]} ({attachment["size"]/1024**2:.2f} МБ)'
                item=QListWidgetItem(f'{row["sender_name"]} · {stamp}\n{body}')
                item.setData(Qt.UserRole,row.get('file'));self.messages.addItem(item)
            if data['messages']:self.messages.scrollToBottom()
            suffix=' · удаление отменённой отправки ожидает связи' if data['pending'] else ''
            self.status.setText(f'{self.display_name} · подключено · {api.url}'+suffix)
        def failed(error):
            self.polling=False
            if self.api is api:self.status.setText('Нет связи с сервером · повторяем подключение…')
        self.job(fetch,received,failed)

    def select_chat(self,index):
        if not self.api or self.transfer or self.sending:return
        key=self.chat_select.itemData(index)
        if key and key!=self.api.chat_id:
            self.api.chat_id=key;self.chat_revision=None;self.cursor=0;self.messages.clear();self.pending=None;self.poll()

    def new_chat(self):
        if not self.api or not self.is_host:return
        title,ok=QInputDialog.getText(self,'Новый чат','Название:')
        if not ok or not title.strip():return
        def created(chat):
            self.api.chat_id=chat['id'];self.chat_revision=None;self.cursor=0;self.messages.clear();self.poll()
        self.job(lambda _:self.api.call('POST','/api/chats',json={'title':title.strip()}),created)

    def settings(self):
        if self.api and self.is_host:
            self.job(lambda _:self.api.call('GET','/api/storage'),lambda data:StorageDialog(self,data).exec())
        else:StorageDialog(self).exec()

    def send(self):
        text = self.text.text()
        if not self.api or not text.strip() or self.sending: return
        if not self.pending or self.pending[0] != text: self.pending = (text, str(uuid.uuid4()))
        self.sending = True; self.controls(); api = self.api
        def done(_):
            self.sending = False; self.pending = None; self.text.clear(); self.controls(); self.poll()
        def failed(error):
            self.sending = False; self.controls(); self.error(error+'\nТекст сохранён. Можно повторить отправку.')
        self.job(lambda _: api.call('POST', '/api/messages', json={'text': text, 'client_id': self.pending[1]}), done, failed)

    def transfer_job(self, task):
        self.transfer=True;self.transfer_control=TransferControl();control=self.transfer_control
        self.cancel_button.setEnabled(True);self.cancel_button.setText('Отменить передачу');self.controls()
        def finish(message):
            self.transfer=False;self.transfer_control=None;self.progress.setText(message);self.controls();self.poll()
        def failed(error):
            finish('Передача не завершена')
            self.error(error+'\nПри обрыве связи можно повторно выбрать тот же файл для докачки.')
        self.job(lambda report:task(report,control),finish,failed,finish)

    def cancel_transfer(self):
        if self.transfer_control and self.transfer_control.cancel():
            self.cancel_button.setEnabled(False);self.cancel_button.setText('Отменяем и удаляем временные данные…')
            self.progress.setText('Отмена передачи…')

    def upload(self):
        if not self.api or self.transfer:return
        filename,_=QFileDialog.getOpenFileName(self,'Выберите файл')
        if filename:
            api=self.api
            self.transfer_job(lambda report,control:api.upload(filename,report,control))

    def download(self,item):
        attachment=item.data(Qt.UserRole)
        if not attachment or not self.api or self.transfer:return
        destination,_=QFileDialog.getSaveFileName(self,'Сохранить файл',attachment['name'])
        if destination:
            api=self.api
            self.transfer_job(lambda report,control:api.download(attachment,destination,report,control))

    def closeEvent(self, event):
        self.timer.stop()
        if self.transfer:self.cancel_transfer()
        if self.jobs:
            self.progress.setText('Завершение текущих операций перед закрытием…')
            QTimer.singleShot(300, self.close)
            event.ignore(); return
        if self.host:
            if QMessageBox.question(self, 'Остановить сервер?', 'Закрытие этого окна остановит сервер для обоих участников.') != QMessageBox.Yes:
                self.timer.start(); event.ignore(); return
            if self.tunnel:
                self.tunnel.terminate()
                if not self.tunnel.waitForFinished(3000): self.tunnel.kill(); self.tunnel.waitForFinished(2000)
            stop(self.host)
        self.timer.stop(); event.accept()


def apply_theme(app):
    font_path=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'/'segoeui.ttf'
    if font_path.is_file():
        QFontDatabase.addApplicationFont(str(font_path))
    app.setFont(QFont('Segoe UI',10))
    app.setStyle('Fusion')
    app.setStyleSheet('''
      QWidget { color: #172033; background-color: #f4f6fa; font-family: "Segoe UI", sans-serif; font-size: 14px; }
      QMainWindow { background: #f4f6fa; }
      QLabel#title { font-size: 30px; font-weight: 700; color: #2546a3; }
      QLabel#warning { background: #fff0ce; color: #704c00; padding: 10px; border-radius: 6px; }
      QPushButton { padding: 9px 14px; border: 1px solid #cbd3e3; border-radius: 6px; background: white; }
      QPushButton:hover { background: #e8efff; }
      QPushButton:disabled { color: #9097a5; }
      QLineEdit { padding: 10px; border: 1px solid #cbd3e3; border-radius: 6px; background: white; }
      QListWidget { background: white; border: 1px solid #dce2ed; border-radius: 8px; }
      QListWidget::item:selected { color: #172033; background: #dce7ff; }
      QLineEdit { selection-color: white; selection-background-color: #2546a3; }
      QListWidget::item { padding: 12px; border-bottom: 1px solid #edf0f6; }
    ''')
def main():
    app = QApplication(sys.argv)
    apply_theme(app)
    window = Window(); window.show()
    sys.exit(app.exec())


if __name__ == '__main__': main()
