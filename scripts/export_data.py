"""Export public rows and coverage. No admin tokens or unpublished candidates."""
import argparse, json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app import store

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',default=str(ROOT/'exports'));a=p.parse_args()
    d=Path(a.out);d.mkdir(parents=True,exist_ok=True);store.init_db()
    (d/'jobs.json').write_text(json.dumps(store.jobs(),ensure_ascii=False,indent=2),encoding='utf-8')
    (d/'coverage.json').write_text(json.dumps(store.metadata(),ensure_ascii=False,indent=2),encoding='utf-8')
    from app.main import export_csv
    (d/'jobs.csv').write_bytes(export_csv().body)
if __name__=='__main__': main()
