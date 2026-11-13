import importlib.util
from pathlib import Path
import sqlite3, subprocess
import pytest
from app import store

@pytest.fixture
def state_module(tmp_path, monkeypatch):
    source=Path(__file__).resolve().parents[1]/'scripts/state_branch.py'
    spec=importlib.util.spec_from_file_location('state_fixture',source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    repo=tmp_path/'repo';repo.mkdir()
    remote=tmp_path/'remote.git'
    subprocess.run(['git','init','--bare',str(remote)],check=True,capture_output=True)
    subprocess.run(['git','init','-b','main',str(repo)],check=True,capture_output=True)
    def git(*args):return subprocess.run(['git',*args],cwd=repo,check=True,capture_output=True)
    git('config','user.name','offline-test');git('config','user.email','test@example.invalid')
    (repo/'README').write_text('Offline test only')
    git('add','README');git('commit','-m','fixture');git('remote','add','origin',str(remote));git('push','origin','main')
    db=tmp_path/'jobs.sqlite3'
    monkeypatch.setattr(module,'ROOT',repo);monkeypatch.setattr(module,'DB_PATH',db)
    monkeypatch.setattr(store,'DB_PATH',db)
    return module,db

def test_state_roundtrip_preserves_database(state_module,monkeypatch):
    module,db=state_module
    monkeypatch.setenv('GITHUB_ACTIONS','true')
    store.init_db(False);store.set_setting('offline_fixture',{'ok':True})
    module.save()
    for p in (db,Path(str(db)+'-wal'),Path(str(db)+'-shm')):p.unlink(missing_ok=True)
    module.restore()
    assert store.setting('offline_fixture')=={'ok':True}
    store.set_setting('second',2);module.save()
    db.unlink();module.restore()
    assert store.setting('second')==2

def test_state_save_refuses_non_ci_context(state_module,monkeypatch):
    module,_=state_module;monkeypatch.delenv('GITHUB_ACTIONS',raising=False)
    with pytest.raises(SystemExit):module.save()

def test_pages_workflow_contains_schedule_and_persistent_state():
    import yaml
    path=Path(__file__).resolve().parents[1]/'.github/workflows/daily-pages.yml'
    d=yaml.load(path.read_text(),Loader=yaml.BaseLoader)
    assert d['on']['schedule'][0]['cron']=='17 20 * * *'
    assert d['permissions']['contents']=='write'
    steps=d['jobs']['collect']['steps']
    assert any(s.get('run')=='python scripts/state_branch.py restore' for s in steps)
    assert any(s.get('run')=='python scripts/state_branch.py save' for s in steps)
    assert d['concurrency']['cancel-in-progress']=='false'