"""Optional temporary HTTPS transport. No account, no router changes."""
import hashlib
from pathlib import Path
import urllib.request
import os

VERSION = '2026.11.0'
URL = 'https://github.com/cloudflare/cloudflared/releases/download/2026.11.0/cloudflared-windows-amd64.exe'
SHA256 = 'd5c1d82031da6253dcdcff64c944cd0fe063016edeb2e4fd088075fc9bd52d0a'
SIZE = 55397592


def prepare(report=lambda message: None):
    target = Path(__file__).resolve().parent / 'tools' / 'cloudflared.exe'
    target.parent.mkdir(exist_ok=True)
    def valid(path):
        if not path.exists() or path.stat().st_size != SIZE:
            return False
        h = hashlib.sha256()
        with path.open('rb') as f:
            while block := f.read(1024*1024):
                h.update(block)
        return h.hexdigest() == SHA256
    if valid(target):
        return str(target)
    report('Загружается официальный cloudflared (53 МБ)…')
    partial = target.with_suffix('.download')
    request = urllib.request.Request(URL, headers={'User-Agent': 'PRISM/0.3.1'})
    done = 0
    with urllib.request.urlopen(request, timeout=30) as response, partial.open('wb') as f:
        while block := response.read(1024*1024):
            done += len(block)
            if done > SIZE:
                raise RuntimeError('Неверный размер сетевого компонента')
            f.write(block)
            report(f'Загрузка сетевого компонента: {done/SIZE:.0%}')
    if not valid(partial):
        raise RuntimeError('Контрольная сумма cloudflared не совпадает. Запуск отменён.')
    os.replace(partial, target)
    return str(target)
