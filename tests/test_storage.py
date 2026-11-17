import hashlib
import sqlite3
import uuid
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pytest
from fastapi.testclient import TestClient
from server import configure,create_app

@pytest.fixture
def workspace(tmp_path):
    headers=[{'Authorization':'Bearer '+u['token']} for u in configure(tmp_path)['users']]
    with TestClient(create_app(tmp_path)) as client:yield client,headers,tmp_path

def send(client,headers,chat='general',text='hello'):
    result=client.post('/api/messages',headers=headers,params={'chat_id':chat},json={'client_id':str(uuid.uuid4()),'text':text})
    assert result.status_code==200
    return result.json()

def upload(client,headers,chat='general',complete=True,data=b'file',key=None):
    key=key or str(uuid.uuid4())
    body={'name':'sample.bin','size':len(data),'sha256':hashlib.sha256(data).hexdigest(),'transfer_id':key}
    assert client.post('/api/uploads',headers=headers,params={'chat_id':chat},json=body).status_code==200
    assert client.put('/api/uploads/'+key,headers=headers,params={'offset':0},content=data if complete else data[:2]).status_code==200
    if complete:assert client.post('/api/uploads/'+key+'/finish',headers=headers).status_code==200
    return key,body

def test_separate_chats_and_host_permissions(workspace):
    client,h,root=workspace
    second=client.post('/api/chats',headers=h[0],json={'title':'Проект'}).json()['id']
    send(client,h[0],text='general')
    send(client,h[1],second,'second')
    assert [r['text'] for r in client.get('/api/messages',headers=h[1]).json()]==['general']
    assert [r['text'] for r in client.get('/api/messages',headers=h[0],params={'chat_id':second}).json()]==['second']
    assert client.get('/api/storage',headers=h[1]).status_code==403
    assert client.post('/api/storage/clear',headers=h[1],json={'mode':'all'}).status_code==403
    assert client.post('/api/chats',headers=h[1],json={'title':'Forbidden'}).status_code==403

def test_cache_does_not_delete_history_or_finished_files(workspace):
    client,h,root=workspace
    send(client,h[0])
    finished,_=upload(client,h[0])
    pending,body=upload(client,h[0],complete=False,data=b'partial')
    metrics=client.get('/api/storage',headers=h[0]).json()['chats'][0]
    assert metrics['cache_bytes']==2 and metrics['file_bytes']==4
    result=client.post('/api/storage/clear',headers=h[0],json={'mode':'cache','chat_id':'general'})
    assert result.status_code==200
    assert not (root/'files'/pending).exists()
    assert (root/'files'/finished).read_bytes()==b'file'
    assert len(client.get('/api/messages',headers=h[0]).json())==2
    # Late begin/chunk requests cannot resurrect a cleared transfer.
    assert client.post('/api/uploads',headers=h[0],json=body).status_code==409
    assert client.put('/api/uploads/'+pending,headers=h[0],params={'offset':0},content=b'x').status_code==404

def test_attachments_and_full_cleanup_are_scoped(workspace):
    client,h,root=workspace
    second=client.post('/api/chats',headers=h[0],json={'title':'Другой'}).json()['id']
    send(client,h[0],text='keep text')
    first,_=upload(client,h[0])
    other,_=upload(client,h[0],chat=second)
    assert client.post('/api/storage/clear',headers=h[0],json={'mode':'files','chat_id':'general'}).status_code==200
    rows=client.get('/api/messages',headers=h[0]).json()
    assert rows[0]['text']=='keep text' and rows[1]['file_id'] is None
    assert not (root/'files'/first).exists() and (root/'files'/other).exists()
    previous=send(client,h[0])['id']
    assert client.post('/api/storage/clear',headers=h[0],json={'mode':'all','chat_id':'general'}).status_code==200
    assert client.get('/api/messages',headers=h[0]).json()==[]
    assert (root/'files'/other).exists()
    assert send(client,h[0])['id']>previous
    assert client.get('/api/chats',headers=h[0]).json()[0]['revision']>=2
    assert client.post('/api/storage/clear',headers=h[0],json={'mode':'all'}).status_code==200
    assert list((root/'files').iterdir())==[]
    assert (root/'access.json').exists()

def test_cancel_ownership_and_tombstones(workspace):
    client,h,root=workspace
    key,_=upload(client,h[1],complete=False)
    assert client.delete('/api/uploads/'+key,headers=h[0]).status_code==403
    assert (root/'files'/key).exists()
    assert client.delete('/api/uploads/'+key,headers=h[1]).status_code==200
    assert client.delete('/api/uploads/'+key,headers=h[1]).status_code==200
    late=str(uuid.uuid4())
    assert client.delete('/api/uploads/'+late,headers=h[1]).status_code==200
    response=client.post('/api/uploads',headers=h[1],json={'name':'late','size':0,'sha256':hashlib.sha256(b'').hexdigest(),'transfer_id':late})
    assert response.status_code==409
    # Cancellation that races with finalization removes the just-published attachment too.
    complete,_=upload(client,h[1])
    assert client.delete('/api/uploads/'+complete,headers=h[1]).status_code==200
    assert client.get('/api/messages',headers=h[0]).json()==[]

def test_old_database_migrates_without_losing_history(tmp_path):
    token=configure(tmp_path)['users'][0]['token']
    with sqlite3.connect(tmp_path/'server.sqlite') as db:
        db.executescript('''CREATE TABLE messages(id INTEGER PRIMARY KEY,sender INTEGER NOT NULL,client_id TEXT NOT NULL,text TEXT NOT NULL,file_id TEXT,created REAL NOT NULL,UNIQUE(sender,client_id));
        CREATE TABLE uploads(id TEXT PRIMARY KEY,owner INTEGER NOT NULL,name TEXT NOT NULL,size INTEGER NOT NULL,sha256 TEXT NOT NULL,finished INTEGER DEFAULT 0);
        INSERT INTO messages VALUES(7,0,'old','old message',NULL,1);''')
    with TestClient(create_app(tmp_path)) as client:
        headers={'Authorization':'Bearer '+token}
        assert client.get('/api/messages',headers=headers).json()[0]['text']=='old message'
        assert send(client,headers)['id']>7
