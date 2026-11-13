"""Command-line tools for local operation and unattended deployments."""
from __future__ import annotations
import argparse, os, secrets, threading, webbrowser
from pathlib import Path

def main():
    p=argparse.ArgumentParser(description='CIVIC PR JOBS')
    sub=p.add_subparsers(dest='command',required=True)
    serve=sub.add_parser('serve'); serve.add_argument('--port',type=int,default=8765); serve.add_argument('--open',action='store_true')
    crawl=sub.add_parser('crawl'); crawl.add_argument('--prefecture'); crawl.add_argument('--limit',type=int); crawl.add_argument('--refresh-registry',action='store_true'); crawl.add_argument('--pages',type=int,default=24); crawl.add_argument('--seconds',type=int,default=None)
    sub.add_parser('registry')
    backup=sub.add_parser('backup'); backup.add_argument('destination')
    args=p.parse_args()
    if args.command=='serve':
        root=Path(__file__).resolve().parents[1]
        token_file=root/'.local_admin_token'
        if not os.getenv('ADMIN_TOKEN'):
            if not token_file.exists():
                token_file.write_text(secrets.token_urlsafe(32)); token_file.chmod(0o600)
            os.environ['ADMIN_TOKEN']=token_file.read_text().strip()
        os.environ.setdefault('SCHEDULER_ENABLED','true')
        os.environ.setdefault('CRAWL_ON_START','true')
        print('\nCIVIC PR JOBS')
        print(f'画面: http://127.0.0.1:{args.port}')
        print('管理トークン（管理画面に入力。第三者に共有しないでください）:')
        print(os.environ['ADMIN_TOKEN'])
        print('このターミナルを閉じると巡回・毎日更新も停止します。停止は Ctrl+C。\n')
        if args.open: threading.Timer(2,lambda:webbrowser.open(f'http://127.0.0.1:{args.port}')).start()
        import uvicorn
        uvicorn.run('app.main:app',host='127.0.0.1',port=args.port,workers=1)
        return
    from . import store
    store.init_db()
    if args.command=='crawl':
        from .crawler import run_crawl
        print(run_crawl(args.prefecture,args.limit,args.refresh_registry,args.pages,max_seconds=args.seconds))
    elif args.command=='registry':
        from .registry import refresh_registry
        print(refresh_registry())
    elif args.command=='backup':
        import sqlite3
        dest=Path(args.destination).expanduser().resolve()
        if dest.exists(): raise SystemExit('出力先が既に存在します。別名を指定してください。')
        with store.connect() as c, sqlite3.connect(dest) as out: c.backup(out)
        print(dest)

if __name__=='__main__': main()
