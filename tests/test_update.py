"""Verify real fast-forward update preserves user files and refuses local edits."""
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
import update


def command(*args):
    return subprocess.run(['git', *(a.as_posix() if isinstance(a,Path) else str(a) for a in args)], check=True, capture_output=True,
                          text=True, encoding='utf-8').stdout.strip()


def commit(root, text):
    (root/'client.py').write_text(text)
    command('-C',root,'add','client.py')
    command('-C',root,'-c','user.name=PRISM test','-c','user.email=test@example.invalid',
            'commit','-m',text)


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    source=tmp_path/'source'
    command('init','-b','main',source)
    commit(source,'initial')
    remote=tmp_path/'origin.git'
    command('clone','--bare',source,remote)
    command('-C',source,'remote','add','origin',remote)
    installed=tmp_path/'installed'
    command('clone',remote,installed)
    monkeypatch.setattr(update,'ROOT',installed)
    return source,installed


def test_update_keeps_data_and_checks_dependencies(checkout, monkeypatch):
    source,installed=checkout
    (installed/'client_state.sqlite').write_bytes(b'private state')
    (installed/'preferences.json').write_text('private settings')
    commit(source,'new version')
    command('-C',source,'push','origin','main')
    original=subprocess.run
    checks=[]
    def run(args,**kwargs):
        if str(args[0])==sys.executable and str(args[1]).endswith('start.pyw'):
            checks.append(args)
            return subprocess.CompletedProcess(args,0)
        return original(args,**kwargs)
    monkeypatch.setattr(subprocess,'run',run)
    update.update()
    assert (installed/'client.py').read_text()=='new version'
    assert (installed/'client_state.sqlite').read_bytes()==b'private state'
    assert (installed/'preferences.json').read_text()=='private settings'
    assert checks[0][-1]=='--check'


def test_local_edits_are_preserved(checkout):
    _,installed=checkout
    (installed/'client.py').write_text('my edits')
    with pytest.raises(RuntimeError,match='локальные изменения'):
        update.update()
    assert (installed/'client.py').read_text()=='my edits'
