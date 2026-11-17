import hashlib
from pathlib import Path
import sys
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from server import CHUNK, configure, create_app


def test_auth_messages_and_restart(tmp_path):
    users = configure(tmp_path)['users']
    headers = [{'Authorization': 'Bearer '+u['token']} for u in users]
    request = {'client_id': str(uuid.uuid4()), 'text': '<b>Привет</b>'}
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/api/messages').status_code == 401
        first = client.post('/api/messages', headers=headers[0], json=request).json()
        duplicate = client.post('/api/messages', headers=headers[0], json=request).json()
        assert first['id'] == duplicate['id']
        assert client.get('/api/messages', headers=headers[1]).json()[0]['text'] == request['text']
        assert client.get('/api/messages', headers=headers[1], params={'after': first['id']}).json() == []
    with TestClient(create_app(tmp_path)) as client:
        assert len(client.get('/api/messages', headers=headers[1]).json()) == 1


def test_files_resume_and_ownership(tmp_path):
    headers = [{'Authorization': 'Bearer '+u['token']} for u in configure(tmp_path)['users']]
    data = b'abc123\x00' * 750000
    body = {'name': '../file.bin', 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    with TestClient(create_app(tmp_path)) as client:
        upload = client.post('/api/uploads', headers=headers[0], json=body).json()
        key = upload['id']; url = '/api/uploads/'+key
        assert client.get('/api/files/'+key, headers=headers[1]).status_code == 404
        assert client.put(url, params={'offset':0}, headers=headers[1], content=b'x').status_code == 404
        assert client.put(url, params={'offset':0}, headers=headers[0], content=data[:CHUNK]).status_code == 200
        assert client.put(url, params={'offset':0}, headers=headers[0], content=b'x').status_code == 409
        assert client.post(url+'/finish', headers=headers[0]).status_code == 409
    with TestClient(create_app(tmp_path)) as client:
        resumed = client.post('/api/uploads', headers=headers[0], json=body).json()
        assert resumed['id'] == key and resumed['offset'] == CHUNK
        assert client.put(url, params={'offset':CHUNK}, headers=headers[0], content=data[CHUNK:]).status_code == 200
        assert client.post(url+'/finish', headers=headers[0]).status_code == 200
        assert client.post(url+'/finish', headers=headers[0]).status_code == 200
        rows = client.get('/api/messages', headers=headers[1]).json()
        assert len(rows) == 1 and rows[0]['file']['name'] == 'file.bin'
        assert client.get('/api/files/'+key, params={'offset':123}, headers=headers[1]).content == data[123:]
        assert client.get('/api/files/'+key, params={'offset':len(data)+1}, headers=headers[1]).status_code == 400


def test_corruption_and_empty_file(tmp_path):
    headers = {'Authorization': 'Bearer '+configure(tmp_path)['users'][0]['token']}
    with TestClient(create_app(tmp_path)) as client:
        key = client.post('/api/uploads', headers=headers, json={
            'name':'bad.bin','size':3,'sha256':hashlib.sha256(b'yes').hexdigest()}).json()['id']
        client.put('/api/uploads/'+key, params={'offset':0}, headers=headers, content=b'bad')
        assert client.post('/api/uploads/'+key+'/finish', headers=headers).status_code == 409
        key = client.post('/api/uploads', headers=headers, json={
            'name':'empty','size':0,'sha256':hashlib.sha256(b'').hexdigest()}).json()['id']
        assert client.post('/api/uploads/'+key+'/finish', headers=headers).status_code == 200
        assert client.get('/api/files/'+key, headers=headers).content == b''


def test_invitation_rules():
    from network import invite, parse_invite
    import pytest
    token = 'a'*43
    assert parse_invite(invite('http://127.0.0.1:8765',token)).token == token
    assert parse_invite(invite('https://chat.example.org',token)).url == 'https://chat.example.org'
    current = invite('https://chat.example.org', token)
    assert current.startswith('prism:')
    assert parse_invite('lantern:' + current.split(':', 1)[1]).token == token
    with pytest.raises(ValueError): parse_invite(invite('http://chat.example.org',token))
    with pytest.raises(ValueError): parse_invite('garbage')
