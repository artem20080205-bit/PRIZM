"""HTTP transport, invitations and resumable disk transfers."""
import base64
import hashlib
import ipaddress
import json
import threading
import uuid
from state import TransferState
from pathlib import Path
from urllib.parse import urlparse
import httpx

CHUNK = 4 * 1024 * 1024
UPLOAD_CHUNK = 256 * 1024
TRANSFER_TIMEOUT = httpx.Timeout(20, read=120, write=120, pool=20)
RETRIES = 5


def transient(error):
    return isinstance(error, httpx.TransportError) or (
        isinstance(error, httpx.HTTPStatusError)
        and error.response.status_code in (408, 429, 500, 502, 503, 504))


def invite(url, token):
    payload = json.dumps({'url': url.rstrip('/'), 'token': token}).encode()
    return 'prism:' + base64.urlsafe_b64encode(payload).decode()


def parse_invite(value):
    try:
        if not value.strip().startswith(('prism:', 'lantern:')) or len(value) > 4096:
            raise ValueError()
        obj = json.loads(base64.urlsafe_b64decode(value.strip().split(':', 1)[1]))
        url, token = obj['url'].rstrip('/'), obj['token']
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise ValueError()
        if not isinstance(token, str) or not 20 <= len(token) <= 256:
            raise ValueError()
        if parsed.scheme == 'http':
            # The unencrypted development mode is limited to local/private addresses.
            host = parsed.hostname
            local = host == 'localhost'
            try:
                address = ipaddress.ip_address(host)
                local = address.is_loopback or address in ipaddress.ip_network('10.0.0.0/8') or address in ipaddress.ip_network('172.16.0.0/12') or address in ipaddress.ip_network('192.168.0.0/16')
            except ValueError:
                pass
            if not local:
                raise ValueError('Для подключения через интернет требуется HTTPS. См. README.')
        return API(url, token)
    except ValueError as e:
        raise ValueError(str(e) or 'Некорректное приглашение') from e
    except Exception as e:
        raise ValueError('Некорректное приглашение') from e


class TransferCancelled(Exception):
    pass

class TransferControl:
    def __init__(self):
        self.event=threading.Event()
        self.lock=threading.Lock()
        self.completed=False
        self.active=None
    def cancel(self):
        with self.lock:
            if self.completed:return False
            self.event.set()
            active=self.active
        if active:
            def close():
                try:active.close()
                except Exception:pass
            threading.Thread(target=close,daemon=True).start()
        return True
    def check(self):
        if self.event.is_set():raise TransferCancelled('Передача отменена')
    def bind(self,client):
        with self.lock:
            self.check()
            self.active=client
    def unbind(self):
        with self.lock:self.active=None
    def commit(self,action=lambda:None):
        with self.lock:
            self.check()
            action()
            self.completed=True


class API:
    def __init__(self, url, token, chat_id='general', state_path=None):
        self.url, self.token = url, token
        self.chat_id=chat_id
        self.profile=hashlib.sha256(token.encode()).hexdigest()
        self.state=TransferState() if state_path is None else TransferState(state_path)

    def client(self):
        return httpx.Client(base_url=self.url, headers={'Authorization': 'Bearer '+self.token},
                            timeout=httpx.Timeout(15, read=120), trust_env=False)

    def call(self, method, path, **kwargs):
        if path in ('/api/messages','/api/uploads'):
            kwargs.setdefault('params',{}).setdefault('chat_id',self.chat_id)
        with self.client() as client:
            response = client.request(method, path, **kwargs)
            if response.is_error:
                try:
                    detail = response.json().get('detail', '')
                except Exception:
                    detail = ''
                raise RuntimeError(f'Сервер: {response.status_code}. {detail}')
            return response.json()

    def flush_cancellations(self):
        for key in self.state.pending(self.profile):
            try:self.call('DELETE','/api/uploads/'+key,timeout=5)
            except Exception:continue
            self.state.acknowledged(self.profile,key)
        return len(self.state.pending(self.profile))

    def transfer_call(self, method, path, control, report, **kwargs):
        # Each attempt uses a new connection: a reset keep-alive is not reusable.
        for attempt in range(RETRIES + 1):
            control.check()
            try:
                with self.client() as client:
                    control.bind(client)
                    try:
                        response = client.request(method, path, timeout=TRANSFER_TIMEOUT, **kwargs)
                        control.check()
                        response.raise_for_status()
                        return response.json()
                    finally:
                        control.unbind()
            except Exception as error:
                control.check()
                if not transient(error) or attempt == RETRIES:
                    raise
                report('Связь прервалась. Восстанавливаем передачу…')
                control.event.wait(min(2 ** attempt, 8))

    def upload(self, filename, report, control=None):
        control=control or TransferControl()
        path=Path(filename)
        before=path.stat()
        digest=hashlib.sha256()
        key=None
        report('Проверка файла…')
        try:
            with path.open('rb') as f:
                while part:=f.read(CHUNK):
                    control.check()
                    digest.update(part)
            control.check()
            signature=hashlib.sha256((self.chat_id+str(path.resolve())+str(before.st_size)+digest.hexdigest()).encode()).hexdigest()
            key=self.state.session(self.profile,signature)
            metadata={'name':path.name,'size':before.st_size,'sha256':digest.hexdigest(),'transfer_id':key}
            def status_request():
                return self.transfer_call('POST','/api/uploads',control,report,
                    params={'chat_id':self.chat_id},json=metadata)
            status=status_request()
            control.check()
            offset=status['offset']
            if not status['finished']:
                with path.open('rb') as f:
                    failures=0
                    while offset < before.st_size:
                        control.check()
                        f.seek(offset)
                        part=f.read(UPLOAD_CHUNK)
                        if not part:raise RuntimeError('Исходный файл изменился во время передачи')
                        try:
                            with self.client() as client:
                                control.bind(client)
                                try:
                                    response=client.put('/api/uploads/'+key,params={'offset':offset},content=part,timeout=TRANSFER_TIMEOUT)
                                    control.check()
                                    response.raise_for_status()
                                    next_offset=response.json()['offset']
                                finally:control.unbind()
                            if next_offset != offset+len(part):
                                raise RuntimeError('Сервер вернул неверную позицию файла')
                            offset=next_offset
                            failures=0
                            report(f'Отправка: {offset/max(before.st_size,1):.0%}')
                        except Exception as error:
                            control.check()
                            conflict=isinstance(error,httpx.HTTPStatusError) and error.response.status_code==409
                            if not (transient(error) or conflict) or failures>=RETRIES:raise
                            failures+=1
                            report('Связь прервалась. Восстанавливаем передачу…')
                            control.event.wait(min(2**(failures-1),8))
                            # The chunk may have arrived even when its reply was lost.
                            # Ask for the durable offset before sending any more bytes.
                            resumed=status_request()
                            position=resumed['offset']
                            if not 0<=position<=before.st_size:
                                raise RuntimeError('Сервер вернул неверную позицию файла')
                            offset=position
                after=path.stat()
                if (after.st_size,after.st_mtime_ns)!=(before.st_size,before.st_mtime_ns):
                    raise RuntimeError('Файл изменился во время передачи')
            control.check()
            report('Сервер проверяет целостность…')
            self.transfer_call('POST','/api/uploads/'+key+'/finish',control,report)
            control.commit()
            self.state.forget(self.profile,key)
            return 'Файл отправлен'
        except Exception:
            if control.event.is_set():
                if key:
                    self.state.cancel(self.profile,key)
                    remaining=self.flush_cancellations()
                    if remaining:
                        raise TransferCancelled('Отменено. Серверная копия будет удалена после восстановления связи.') from None
                raise TransferCancelled('Отменено. Незавершённая серверная копия удалена.') from None
            raise

    def download(self, attachment, destination, report, control=None):
        control=control or TransferControl()
        dest=Path(destination)
        partial=dest.with_name(dest.name+'.'+attachment['id']+'.part')
        offset=partial.stat().st_size if partial.exists() else 0
        if offset>attachment['size']:raise RuntimeError('Временный файл больше исходного')
        try:
            control.check()
            with self.client() as client:
                control.bind(client)
                try:
                    with client.stream('GET','/api/files/'+attachment['id'],params={'offset':offset},timeout=10) as response:
                        response.raise_for_status()
                        with partial.open('ab') as f:
                            for part in response.iter_bytes(64*1024):
                                control.check()
                                f.write(part)
                                offset+=len(part)
                                report(f'Скачивание: {offset/max(attachment["size"],1):.0%}')
                finally:control.unbind()
            control.check()
            digest=hashlib.sha256()
            report('Проверка скачанного файла…')
            with partial.open('rb') as f:
                while part:=f.read(CHUNK):
                    control.check()
                    digest.update(part)
            if offset!=attachment['size'] or digest.hexdigest()!=attachment['sha256']:
                partial.unlink(missing_ok=True)
                raise RuntimeError('Файл повреждён. Неверная временная копия удалена.')
            control.commit(lambda:partial.replace(dest))
            return 'Сохранено: '+str(dest)
        except Exception:
            if control.event.is_set():
                # Only this transfer's sidecar is removed, never the original or destination.
                partial.unlink(missing_ok=True)
                raise TransferCancelled('Скачивание отменено. Временная копия удалена.') from None
            raise
