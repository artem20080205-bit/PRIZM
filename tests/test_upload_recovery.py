"""Regression: friend upload survives reset before/after server acknowledgement."""
import httpx
import pytest
from test_cancellation import live, source
from network import API, UPLOAD_CHUNK
from server import configure


@pytest.mark.parametrize('accepted', [False, True])
def test_friend_upload_recovers_reset(live, monkeypatch, accepted):
    host, root = live
    friend = API(host.url, configure(root/'server')['users'][1]['token'],
                 state_path=root/'friend.sqlite')
    path = source(root)
    original = httpx.Client.put
    offsets = []
    broken = False

    def reset(client, url, **kwargs):
        nonlocal broken
        assert len(kwargs['content']) <= UPLOAD_CHUNK
        offsets.append(kwargs['params']['offset'])
        if not broken:
            broken = True
            if accepted:
                assert original(client, url, **kwargs).status_code == 200
            raise httpx.ReadError('[WinError 10054] connection reset')
        return original(client, url, **kwargs)

    monkeypatch.setattr(httpx.Client, 'put', reset)
    friend.upload(path, lambda _: None)
    assert offsets[1] == (UPLOAD_CHUNK if accepted else 0)
    messages = host.call('GET', '/api/messages')
    assert len(messages) == 1
    assert messages[0]['sender'] == 1
    destination = root/'received.bin'
    host.download(messages[0]['file'], destination, lambda _: None)
    assert destination.read_bytes() == path.read_bytes()


def test_permanent_reset_is_bounded_and_resumable(live, monkeypatch):
    api, root = live
    path = source(root)
    original = httpx.Client.put
    count = 0

    def reset(client, url, **kwargs):
        nonlocal count
        count += 1
        raise httpx.WriteError('connection reset')

    import network
    monkeypatch.setattr(network, 'RETRIES', 1)
    monkeypatch.setattr(httpx.Client, 'put', reset)
    with pytest.raises(httpx.WriteError):
        api.upload(path, lambda _: None)
    assert count == 2
    assert api.call('GET', '/api/messages') == []
    monkeypatch.setattr(httpx.Client, 'put', original)
    api.upload(path, lambda _: None)
    assert len(api.call('GET', '/api/messages')) == 1
