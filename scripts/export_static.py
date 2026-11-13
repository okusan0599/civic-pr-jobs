"""Build an honest, offline screen preview with a fixed reference date."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
from datetime import datetime
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app import store

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--out',default=str(ROOT/'Preview.html'))
    parser.add_argument('--hosted',action='store_true')
    args=parser.parse_args()
    store.init_db()
    meta=store.metadata(); meta.update(mode='published_snapshot' if args.hosted else 'snapshot',scheduler_enabled=False,daily_hour_jst=5,external_schedule='GitHub Actions 05:17 JST' if args.hosted else None)
    rows=store.jobs()
    payload=json.dumps({'jobs':rows,'meta':meta,'sources':store.sources()},ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    html=(ROOT/'web'/'index.html').read_text(encoding='utf-8')
    html=html.replace('<script id="embedded-data" type="application/json">null</script>', '<script id="embedded-data" type="application/json">'+payload+'</script>')
    Path(args.out).write_text(html,encoding='utf-8')
    print(args.out)
if __name__=='__main__': main()
