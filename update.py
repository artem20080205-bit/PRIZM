"""Update a Git checkout without deleting data or discarding local edits."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def git(*args):
    return subprocess.run(['git', '-C', str(ROOT), *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding='utf-8', errors='replace', timeout=180).stdout.strip()


def update():
    if not (ROOT/'.git').exists():
        raise RuntimeError('Эта папка распакована из архива. Для обновлений нужна копия, полученная командой git clone.')
    if git('status', '--porcelain', '--untracked-files=no'):
        raise RuntimeError('В коде есть локальные изменения. Сохраните их в Git или разберите вручную; обновление остановлено.')
    # Use the installed environment to detect this checkout's running Qt client/server.
    python = ROOT/'.venv313'/'Scripts'/'python.exe'
    if python.exists():
        check = '''import pathlib,psutil,sys
root=pathlib.Path(sys.argv[1]).resolve()
for p in psutil.process_iter(['cmdline','cwd']):
    try:
        args=p.info['cmdline'] or []
        for arg in args[1:]:
            if pathlib.Path(arg).name.lower() not in ('client.py','server.py'):continue
            path=pathlib.Path(arg)
            if not path.is_absolute():path=pathlib.Path(p.info['cwd'] or '')/path
            if path.resolve().parent==root:sys.exit(2)
    except (psutil.Error,OSError):pass
'''
        result = subprocess.run([str(python), '-c', check, str(ROOT)], timeout=30)
        if result.returncode:
            raise RuntimeError('Закройте PRISM и его сервер перед обновлением. Если приложение закрыто, проверьте установку зависимостей.')
    before = git('rev-parse', 'HEAD')
    print('Скачиваем обновление из GitHub…', flush=True)
    print(git('pull', '--ff-only'))
    after = git('rev-parse', 'HEAD')
    print('Проверяем зависимости…', flush=True)
    subprocess.run([sys.executable, str(ROOT/'start.pyw'), '--check'], check=True)
    print('Установлена версия кода: '+after[:12], flush=True)
    print('Обновление готово.' if before != after else 'У вас уже актуальная версия.')
    print('Запустите start.pyw. Переписка, вложения и настройки сохранены.')


if __name__ == '__main__':
    try:
        update()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print('Обновление не завершено: '+str(error), file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr, file=sys.stderr)
        print('Локальные изменения и данные не удалялись. После ошибки установки зависимостей повторите update.cmd.', file=sys.stderr)
        sys.exit(1)
