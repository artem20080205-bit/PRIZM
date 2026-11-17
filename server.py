"""PRISM transport prototype. NO end-to-end encryption. Single process only."""
import argparse
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
import uuid

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

CHUNK = 4 * 1024 * 1024


def configure(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    config = folder / 'access.json'
    if not config.exists():
        value = {'users': [{'name': name, 'token': secrets.token_urlsafe(32)}
                           for name in ['Участник 1', 'Участник 2']]}
        # Credentials remain private to the server administrator.
        fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
    return json.loads(config.read_text(encoding='utf-8'))


class Text(BaseModel):
    client_id: uuid.UUID
    text: str = Field(min_length=1, max_length=16000)


class Upload(BaseModel):
    name: str = Field(min_length=1, max_length=240)
    size: int = Field(ge=0, le=2**63-1)
    transfer_id: uuid.UUID | None = None
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class ChatCreate(BaseModel):
    title: str = Field(min_length=1, max_length=80)

class Clean(BaseModel):
    mode: str = Field(pattern=r'^(cache|files|all)$')
    chat_id: str | None = None


def create_app(folder):
    root = Path(folder)
    users = configure(root)['users']
    (root / 'files').mkdir(exist_ok=True)
    lock = threading.RLock()
    db = sqlite3.connect(root / 'server.sqlite', check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript('''
      PRAGMA journal_mode=WAL;
      CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY, sender INTEGER NOT NULL, client_id TEXT NOT NULL,
        text TEXT NOT NULL, file_id TEXT, created REAL NOT NULL,
        UNIQUE(sender,client_id));
      CREATE TABLE IF NOT EXISTS uploads (
        id TEXT PRIMARY KEY, owner INTEGER NOT NULL, name TEXT NOT NULL,
        size INTEGER NOT NULL, sha256 TEXT NOT NULL, finished INTEGER DEFAULT 0);
    ''')
    # Additive migration preserves existing version 0.2 history.
    for table in ('messages','uploads'):
        if 'chat_id' not in [r[1] for r in db.execute('PRAGMA table_info('+table+')')]:
            db.execute("ALTER TABLE "+table+" ADD COLUMN chat_id TEXT NOT NULL DEFAULT 'general'")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS chats(id TEXT PRIMARY KEY,title TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0);
      INSERT OR IGNORE INTO chats(id,title) VALUES('general','Общий чат');
      CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY,value INTEGER NOT NULL);
      INSERT OR IGNORE INTO counters(name,value) SELECT 'messages',COALESCE(MAX(id),0) FROM messages;
      CREATE TABLE IF NOT EXISTS cancelled(id TEXT PRIMARY KEY,owner INTEGER NOT NULL);
    """)
    db.commit()
    readers = {}
    finalizers = {}

    @asynccontextmanager
    async def lifespan(app):
        yield
        db.close()

    app = FastAPI(title='PRISM prototype', docs_url=None, redoc_url=None, lifespan=lifespan)

    def auth(request: Request):
        token = request.headers.get('Authorization', '').removeprefix('Bearer ')
        if len(token) > 256:
            raise HTTPException(401, 'Неверное приглашение')
        for index, user in enumerate(users):
            if secrets.compare_digest(token.encode(), user['token'].encode()):
                return index
        raise HTTPException(401, 'Неверное приглашение')

    def admin(user):
        if user != 0:
            raise HTTPException(403,'Настройки сервера доступны только хозяину')

    def chat(key):
        row=db.execute('SELECT * FROM chats WHERE id=?',(key,)).fetchone()
        if row is None: raise HTTPException(404,'Чат не найден')
        return row

    def next_message():
        db.execute("UPDATE counters SET value=value+1 WHERE name='messages'")
        return db.execute("SELECT value FROM counters WHERE name='messages'").fetchone()[0]

    def revision(key):
        db.execute('UPDATE chats SET revision=revision+1 WHERE id=?',(key,))

    def remove_files(rows):
        # Only explicit DB-owned UUID paths under this storage are deleted.
        for row in rows:
            if row['id'] in finalizers:finalizers[row['id']].set()
        if any(row['id'] in finalizers for row in rows):
            raise HTTPException(409,'Отменяем проверку файла. Повторите очистку через несколько секунд.')
        if any(readers.get(row['id'],0) for row in rows):
            raise HTTPException(409,'Файл сейчас скачивается. Повторите очистку после завершения передачи.')
        targets=[]
        for row in rows:
            target=path_for(row['id'])
            if target.resolve().parent != (root/'files').resolve():
                raise HTTPException(400,'Небезопасный путь файла')
            targets.append(target)
        for target in targets:
            target.unlink(missing_ok=True)

    @app.get('/api/chats')
    def list_chats(user=Depends(auth)):
        with lock:
            return [dict(row) for row in db.execute('SELECT * FROM chats ORDER BY rowid')]

    @app.post('/api/chats')
    def add_chat(body:ChatCreate,user=Depends(auth)):
        admin(user)
        title=body.title.strip()
        if not title: raise HTTPException(400,'Введите название чата')
        with lock,db:
            key=str(uuid.uuid4())
            db.execute('INSERT INTO chats(id,title) VALUES(?,?)',(key,title))
            return dict(chat(key))

    @app.get('/api/storage')
    def storage(user=Depends(auth)):
        admin(user)
        with lock:
            items=[]
            for row in db.execute('SELECT * FROM chats ORDER BY rowid').fetchall():
                item=dict(row)
                files=db.execute('SELECT * FROM uploads WHERE chat_id=?',(row['id'],)).fetchall()
                item.update(cache_bytes=0,file_bytes=0,cache_count=0,file_count=0)
                for f in files:
                    size=path_for(f['id']).stat().st_size if path_for(f['id']).exists() else 0
                    prefix='file' if f['finished'] else 'cache'
                    item[prefix+'_bytes']+=size
                    item[prefix+'_count']+=1
                item['messages']=db.execute('SELECT COUNT(*) FROM messages WHERE chat_id=?',(row['id'],)).fetchone()[0]
                items.append(item)
            return {'folder':str(root.resolve()),'chats':items}

    @app.post('/api/storage/clear')
    def clear(body:Clean,user=Depends(auth)):
        admin(user)
        with lock,db:
            keys=[body.chat_id] if body.chat_id is not None else [r['id'] for r in db.execute('SELECT id FROM chats')]
            for key in keys:chat(key)
            rows=[]
            for key in keys:
                query='SELECT * FROM uploads WHERE chat_id=?'
                if body.mode=='cache':query+=' AND finished=0'
                rows.extend(db.execute(query,(key,)).fetchall())
            remove_files(rows)
            for row in rows:
                db.execute('INSERT OR IGNORE INTO cancelled(id,owner) VALUES(?,?)',(row['id'],row['owner']))
                db.execute("UPDATE messages SET text='[Вложение удалено хозяином сервера]',file_id=NULL WHERE file_id=?",(row['id'],))
                db.execute('DELETE FROM uploads WHERE id=?',(row['id'],))
            for key in keys:
                if body.mode=='all':db.execute('DELETE FROM messages WHERE chat_id=?',(key,))
                revision(key)
            return {'ok':True,'deleted_files':len(rows)}

    @app.delete('/api/uploads/{file_id}')
    def cancel_upload(file_id:uuid.UUID,user=Depends(auth)):
        key=str(file_id)
        with lock,db:
            row=db.execute('SELECT * FROM uploads WHERE id=?',(key,)).fetchone()
            if row is not None and row['owner']!=user:
                raise HTTPException(403,'Нельзя отменять чужую отправку')
            previous=db.execute('SELECT owner FROM cancelled WHERE id=?',(key,)).fetchone()
            if previous is not None and previous['owner']!=user:
                raise HTTPException(403,'Нет доступа')
            if row is not None:
                remove_files([row])
                db.execute('DELETE FROM messages WHERE file_id=?',(key,))
                db.execute('DELETE FROM uploads WHERE id=?',(key,))
                revision(row['chat_id'])
            # Tombstone prevents an in-flight begin request from recreating the file.
            db.execute('INSERT OR IGNORE INTO cancelled(id,owner) VALUES(?,?)',(key,user))
            return {'ok':True}

    def message(row):
        value = dict(row)
        value['sender_name'] = users[value['sender']]['name']
        if value['file_id']:
            attachment = db.execute('SELECT id,name,size,sha256 FROM uploads WHERE id=?',
                                    (value['file_id'],)).fetchone()
            value['file'] = dict(attachment)
        return value

    def upload_row(file_id, owner=None):
        row = db.execute('SELECT * FROM uploads WHERE id=?', (file_id,)).fetchone()
        if row is None or (owner is not None and row['owner'] != owner):
            raise HTTPException(404, 'Файл не найден')
        return row

    def path_for(file_id):
        # Never use a user-supplied name as a server path.
        return root / 'files' / file_id

    @app.get('/api/me')
    def me(user=Depends(auth)):
        return {'id': user, 'name': users[user]['name'], 'prototype': True,
                'end_to_end_encryption': False, 'is_host': user == 0, 'protocol': 3}

    @app.get('/api/messages')
    def messages(after: int = 0, chat_id: str = 'general', user=Depends(auth)):
        if after < 0:
            raise HTTPException(400, 'Некорректный курсор')
        with lock:
            chat(chat_id)
            return [message(r) for r in db.execute(
                'SELECT * FROM messages WHERE id>? AND chat_id=? ORDER BY id LIMIT 200', (after,chat_id))]

    @app.post('/api/messages')
    def send(body: Text, chat_id: str = 'general', user=Depends(auth)):
        if not body.text.strip():
            raise HTTPException(400, 'Пустое сообщение')
        with lock, db:
            chat(chat_id)
            db.execute('INSERT OR IGNORE INTO messages(id,sender,client_id,text,created,chat_id) VALUES(?,?,?,?,?,?)',
                       (next_message(), user, str(body.client_id), body.text, time.time(),chat_id))
            return message(db.execute('SELECT * FROM messages WHERE sender=? AND client_id=?',
                                      (user, str(body.client_id))).fetchone())

    @app.post('/api/uploads')
    def begin(body: Upload, chat_id: str = 'general', user=Depends(auth)):
        name = body.name.replace('\\', '/').split('/')[-1]
        if name in ('', '.', '..') or any(ord(c) < 32 for c in name):
            raise HTTPException(400, 'Некорректное имя')
        with lock, db:
            chat(chat_id)
            if body.transfer_id:
                file_id=str(body.transfer_id)
                if db.execute('SELECT 1 FROM cancelled WHERE id=?',(file_id,)).fetchone():
                    raise HTTPException(409,'Передача отменена')
                row=db.execute('SELECT * FROM uploads WHERE id=?',(file_id,)).fetchone()
                if row and (row['owner']!=user or row['chat_id']!=chat_id or row['sha256']!=body.sha256 or row['size']!=body.size):
                    raise HTTPException(409,'Конфликт передачи')
            else:
                file_id=str(uuid.uuid4())
                row=db.execute('SELECT * FROM uploads WHERE owner=? AND chat_id=? AND name=? AND size=? AND sha256=? ORDER BY finished DESC LIMIT 1',
                    (user,chat_id,name,body.size,body.sha256)).fetchone()
            if row is None:
                path_for(file_id).touch()
                db.execute('INSERT INTO uploads(id,owner,name,size,sha256,chat_id) VALUES(?,?,?,?,?,?)',
                    (file_id,user,name,body.size,body.sha256,chat_id))
                row=upload_row(file_id)
            return {'id': row['id'], 'offset': path_for(row['id']).stat().st_size,
                    'finished': bool(row['finished'])}

    @app.put('/api/uploads/{file_id}')
    async def chunk(file_id: uuid.UUID, request: Request, offset: int, user=Depends(auth)):
        if offset < 0:
            raise HTTPException(400, 'Некорректная позиция')
        data = bytearray()
        async for part in request.stream():
            if len(data) + len(part) > CHUNK:
                raise HTTPException(413, 'Слишком большой блок')
            data.extend(part)
        key = str(file_id)
        with lock:
            row = upload_row(key, user)
            path = path_for(key)
            if row['finished'] or offset != path.stat().st_size:
                raise HTTPException(409, 'Повторите отправку файла для докачки')
            if offset + len(data) > row['size']:
                raise HTTPException(400, 'Превышен размер файла')
            with path.open('ab') as f:
                f.write(data)
            return {'offset': path.stat().st_size}

    @app.post('/api/uploads/{file_id}/finish')
    def finish(file_id: uuid.UUID, user=Depends(auth)):
        key=str(file_id)
        with lock:
            row=upload_row(key,user)
            if row['finished']:return {'ok':True}
            if key in finalizers:raise HTTPException(409,'Файл уже проверяется')
            path=path_for(key)
            if path.stat().st_size!=row['size']:raise HTTPException(409,'Файл ещё не загружен полностью')
            cancel=threading.Event();finalizers[key]=cancel
        try:
            digest=hashlib.sha256()
            with path.open('rb') as f:
                while part:=f.read(CHUNK):
                    if cancel.is_set():raise HTTPException(409,'Проверка файла отменена')
                    digest.update(part)
            with lock,db:
                if cancel.is_set():raise HTTPException(409,'Проверка файла отменена')
                row=upload_row(key,user)
                if digest.hexdigest()!=row['sha256']:raise HTTPException(409,'Контрольная сумма не совпадает')
                db.execute('UPDATE uploads SET finished=1 WHERE id=?',(key,))
                db.execute('INSERT OR IGNORE INTO messages(id,sender,client_id,text,file_id,created,chat_id) VALUES(?,?,?,?,?,?,?)',
                    (next_message(),user,'file:'+key,'',key,time.time(),row['chat_id']))
            return {'ok':True}
        finally:
            with lock:finalizers.pop(key,None)

    @app.get('/api/files/{file_id}')
    def download(file_id: uuid.UUID, offset: int = 0, user=Depends(auth)):
        key = str(file_id)
        with lock:
            row = upload_row(key)
            if not row['finished']:
                raise HTTPException(404, 'Файл ещё не готов')
            if not 0 <= offset <= row['size']:
                raise HTTPException(400, 'Некорректная позиция')
            remaining = row['size'] - offset
            handle=path_for(key).open('rb')
            readers[key]=readers.get(key,0)+1
        def stream():
            try:
                with handle as f:
                    f.seek(offset)
                    while part := f.read(CHUNK):
                        yield part
            finally:
                with lock:
                    readers[key]=max(0,readers.get(key,1)-1)
        return StreamingResponse(stream(), media_type='application/octet-stream',
                                 headers={'Content-Length': str(remaining)})

    return app


if __name__ == '__main__':
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', default='server_data')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    uvicorn.run(create_app(args.data), host=args.host, port=args.port)
