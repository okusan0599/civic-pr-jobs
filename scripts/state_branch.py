"""Persist public crawl state in a dedicated branch; never stores admin tokens."""
from __future__ import annotations
import argparse, gzip, os, sqlite3, subprocess, sys, tempfile
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app import store
from app.config import DB_PATH
BRANCH='crawl-state'
REF='refs/remotes/origin/'+BRANCH

def git(*args, data=None, check=True):
    return subprocess.run(['git',*args],cwd=ROOT,input=data,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=check)

def restore():
    exists=git('ls-remote','--heads','origin','refs/heads/'+BRANCH)
    if not exists.stdout.strip():
        print('No previous crawl-state branch. Starting with bundled research data.'); return
    git('fetch','origin',f'{BRANCH}:{REF}')
    blob=git('show',REF+':jobs.sqlite3.gz').stdout
    raw=gzip.decompress(blob)
    if len(raw)>512*1024*1024: raise RuntimeError('State exceeds 512 MB. Migrate to persistent database hosting.')
    temp=DB_PATH.with_suffix('.restore')
    temp.write_bytes(raw)
    try:
        with closing(sqlite3.connect(temp)) as db:
            if db.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise RuntimeError('Invalid restored database')
        for sidecar in (str(DB_PATH)+'-wal',str(DB_PATH)+'-shm'):
            if Path(sidecar).exists(): raise RuntimeError('Refusing restore while a live WAL may exist')
        temp.replace(DB_PATH)
    finally:
        temp.unlink(missing_ok=True)
    print('Previous collection state restored.')

def save():
    if os.getenv('GITHUB_ACTIONS')!='true':
        raise SystemExit('This command writes the crawl-state branch and is restricted to GitHub Actions.')
    store.init_db()
    with tempfile.TemporaryDirectory() as td:
        snapshot=Path(td)/'jobs.sqlite3'
        with store.connect() as src, closing(sqlite3.connect(snapshot)) as dest: src.backup(dest)
        compressed=gzip.compress(snapshot.read_bytes(),mtime=0)
    if len(compressed)>45*1024*1024: raise RuntimeError('State is too large for the branch backend. Use persistent disk hosting.')
    blob=git('hash-object','-w','--stdin',data=compressed).stdout.decode().strip()
    tree=git('mktree',data=f'100644 blob {blob}\tjobs.sqlite3.gz\n'.encode()).stdout.decode().strip()
    parent=git('rev-parse','--verify',REF,check=False)
    args=['-c','user.name=civic-pr-collector','-c','user.email=collector@users.noreply.github.com','commit-tree',tree]
    if parent.returncode==0: args+=['-p',parent.stdout.decode().strip()]
    args+=['-m','Update public recruitment crawl state']
    commit=git(*args).stdout.decode().strip()
    git('push','origin',f'{commit}:refs/heads/{BRANCH}')
    print('Public crawl state saved. No credentials are stored in this branch.')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['restore','save']);a=p.parse_args()
    restore() if a.action=='restore' else save()
