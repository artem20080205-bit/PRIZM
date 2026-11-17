"""Double-click launcher: selects Python 3.13/3.12 and prepares local dependencies."""
from pathlib import Path
import ctypes
import hashlib
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
HIDDEN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

def alert(text):
    ctypes.windll.user32.MessageBoxW(None, text, 'PRISM', 0x10)

def main():
    if not ((3,12) <= sys.version_info[:2] <= (3,13)):
        for version in ('-3.13','-3.12'):
            try:
                result = subprocess.run(['py',version,str(Path(__file__).resolve()),*sys.argv[1:]], creationflags=HIDDEN)
                if result.returncode == 0:
                    return
            except OSError:
                continue
        raise RuntimeError('Нужен Python 3.13 или 3.12 x64. Установите его с python.org вместе с Python Launcher.')
    executable = ROOT / '.venv313' / 'Scripts' / 'python.exe'
    with (ROOT/'launcher.log').open('w',encoding='utf8') as log:
        if not executable.exists():
            subprocess.run([sys.executable,'-m','venv',str(ROOT/'.venv313')],check=True,stdout=log,stderr=log,creationflags=HIDDEN)
        requirements=ROOT/'requirements.txt'
        fingerprint=hashlib.sha256(requirements.read_bytes()).hexdigest()
        marker=ROOT/'.venv313'/'requirements.sha256'
        installed = marker.exists() and marker.read_text() == fingerprint
        if not installed:
            subprocess.run([str(executable),'-m','pip','install','-r',str(requirements)],check=True,stdout=log,stderr=log,creationflags=HIDDEN)
            marker.write_text(fingerprint)
        if '--check' in sys.argv:
            subprocess.run([str(executable),'-c','import PySide6,fastapi,httpx,psutil'],check=True,stdout=log,stderr=log,creationflags=HIDDEN)
            return
        subprocess.run([str(executable),str(ROOT/'client.py')],cwd=str(ROOT),check=True,stdout=log,stderr=log,creationflags=HIDDEN)

if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        alert('Не удалось запустить приложение: '+str(error)+'\nПодробности: '+str(ROOT/'launcher.log'))
        sys.exit(1)
