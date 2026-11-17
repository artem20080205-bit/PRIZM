"""Storage settings and explicitly scoped destructive actions."""
from pathlib import Path
import json
from PySide6.QtCore import Qt,QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLabel,QLineEdit,QPushButton,
    QFileDialog,QGroupBox,QScrollArea,QWidget,QMessageBox)

PREFERENCES=Path(__file__).resolve().parent/'preferences.json'

def preferred_folder():
    try:return json.loads(PREFERENCES.read_text(encoding='utf8'))['server_folder']
    except (OSError,ValueError,KeyError):return str(Path(__file__).resolve().parent/'server_data_v02')

def save_folder(folder):
    path=Path(folder).expanduser().resolve()
    PREFERENCES.write_text(json.dumps({'server_folder':str(path)},ensure_ascii=False,indent=2),encoding='utf8')

def human(size):
    for unit in ['Б','КиБ','МиБ','ГиБ','ТиБ']:
        if size<1024:return f'{size:.1f} {unit}'
        size/=1024
    return f'{size:.1f} ПиБ'

class StorageDialog(QDialog):
    def __init__(self,window,data=None):
        super().__init__(window)
        self.window=window
        self.setWindowTitle('Настройки — хранилище')
        self.resize(780,580)
        layout=QVBoxLayout(self)
        layout.addWidget(QLabel('Папка серверных данных (для следующего запуска сервера):'))
        row=QHBoxLayout()
        self.folder=QLineEdit(preferred_folder())
        choose=QPushButton('Выбрать…')
        choose.clicked.connect(self.choose)
        save=QPushButton('Сохранить путь')
        save.clicked.connect(self.save)
        row.addWidget(self.folder,1);row.addWidget(choose);row.addWidget(save)
        layout.addLayout(row)
        note=QLabel('Смена пути не переносит и не удаляет старые данные. Чтобы открыть прежнюю историю, выберите её папку при запуске сервера.')
        note.setWordWrap(True);layout.addWidget(note)
        self.actions=[]
        if data:
            actual=QLabel('Сейчас сервер хранит данные в: '+data['folder'])
            actual.setWordWrap(True);actual.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(actual)
            if window.host:
                open_folder=QPushButton('Открыть текущую папку')
                open_folder.clicked.connect(lambda:QDesktopServices.openUrl(QUrl.fromLocalFile(data['folder'])))
                layout.addWidget(open_folder)
            description=QLabel('Кэш — только незавершённые отправки. «Удалить вложения» удаляет файлы с сервера для всех участников. «Удалить всё» удаляет также сообщения. Скачанные участниками копии не затрагиваются.')
            description.setWordWrap(True);layout.addWidget(description)
            scroll=QScrollArea();scroll.setWidgetResizable(True)
            panel=QWidget();items=QVBoxLayout(panel)
            for chat in data['chats']:
                self.add_group(items,chat['title'],chat,chat['id'])
            totals={'cache_bytes':sum(c['cache_bytes'] for c in data['chats']),
                'file_bytes':sum(c['file_bytes'] for c in data['chats']),
                'messages':sum(c['messages'] for c in data['chats'])}
            self.add_group(items,'Все чаты на сервере',totals,None)
            items.addStretch();scroll.setWidget(panel);layout.addWidget(scroll,1)
        else:
            text=QLabel('Очистка серверных данных доступна только хозяину после подключения к серверу. Участники не могут удалять серверную историю.')
            text.setWordWrap(True);layout.addWidget(text)
        self.status=QLabel('');self.status.setWordWrap(True);layout.addWidget(self.status)
        self.close_button=QPushButton('Закрыть');self.close_button.clicked.connect(self.accept)
        layout.addWidget(self.close_button)
        self.busy=False

    def choose(self):
        path=QFileDialog.getExistingDirectory(self,'Папка серверных данных',self.folder.text())
        if path:self.folder.setText(path)
    def save(self):
        try:save_folder(self.folder.text())
        except Exception as error:QMessageBox.warning(self,'Не удалось сохранить',str(error));return
        self.status.setText('Путь сохранён. Он будет предложен при следующем запуске сервера.')
    def add_group(self,layout,title,chat,key):
        group=QGroupBox(title if len(title)<=48 else title[:45]+'…');group.setToolTip(title);body=QVBoxLayout(group)
        body.addWidget(QLabel(f'Кэш: {human(chat["cache_bytes"])} · Вложения: {human(chat["file_bytes"])} · Сообщений: {chat["messages"]}'))
        row=QHBoxLayout()
        for label,mode in [('Очистить кэш','cache'),('Удалить вложения','files'),('Удалить всё','all')]:
            button=QPushButton(label)
            button.clicked.connect(lambda checked=False,k=key,m=mode,t=title:self.clear(k,m,t))
            row.addWidget(button);self.actions.append(button)
        body.addLayout(row);layout.addWidget(group)
    def clear(self,key,mode,title):
        if self.busy:return
        descriptions={'cache':'Удалить незавершённые отправки? Переписка и готовые вложения сохранятся.',
            'files':'Удалить готовые вложения и незавершённые отправки с сервера? Это затронет всех участников. Текст сообщений сохранится.',
            'all':'Удалить ВСЮ переписку, вложения и временные файлы? Это затронет всех участников и необратимо.'}
        if QMessageBox.question(self,'Подтверждение очистки',title+'\n\n'+descriptions[mode],QMessageBox.Yes|QMessageBox.No,QMessageBox.No)!=QMessageBox.Yes:return
        self.busy=True
        for action in self.actions:action.setEnabled(False)
        self.close_button.setEnabled(False)
        self.status.setText('Очищаем…')
        def finish(message):
            self.busy=False
            self.close_button.setEnabled(True)
            self.status.setText(message+' Закройте и откройте настройки для обновления размеров.')
            for action in self.actions:action.setEnabled(True)
            self.window.poll()
        self.window.job(lambda _:self.window.api.call('POST','/api/storage/clear',json={'mode':mode,'chat_id':key}),
            lambda result:finish('Очистка завершена. Файлов удалено: '+str(result['deleted_files'])),
            lambda error:finish('Ошибка: '+error))
    def reject(self):
        if not self.busy:super().reject()
    def closeEvent(self,event):
        if self.busy:event.ignore()
        else:super().closeEvent(event)
