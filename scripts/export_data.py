"""Export public rows and coverage. No admin tokens or unpublished candidates."""
import argparse, json, sys, csv, io
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app import store

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',default=str(ROOT/'exports'));a=p.parse_args()
    d=Path(a.out);d.mkdir(parents=True,exist_ok=True);store.init_db()
    (d/'jobs.json').write_text(json.dumps(store.jobs(),ensure_ascii=False,indent=2),encoding='utf-8')
    (d/'coverage.json').write_text(json.dumps(store.metadata(),ensure_ascii=False,indent=2),encoding='utf-8')
    (d/'sources.json').write_text(json.dumps(store.sources(),ensure_ascii=False,indent=2),encoding='utf-8')
    from app.audit import source_run_history
    (d/'crawl-log.json').write_text(json.dumps(source_run_history(),ensure_ascii=False,indent=2),encoding='utf-8')
    columns=['name','pref','kind','url','stage','last_checked','last_hub_success','last_hub_url',
        'attempted_pages','fetched_pages','parsed_pages','failed_pages','pending_pages','retry_pages',
        'new_candidates','updated_candidates','finish_reason','error_details']
    stream=io.StringIO();writer=csv.writer(stream);writer.writerow(columns)
    for row in store.sources():
        cells=[]
        for key in columns:
            value=row.get(key,'')
            text=json.dumps(value,ensure_ascii=False) if isinstance(value,(dict,list)) else str('' if value is None else value)
            if text.lstrip().startswith(('=','+','-','@')): text="'"+text
            cells.append(text)
        writer.writerow(cells)
    (d/'sources.csv').write_text('\ufeff'+stream.getvalue(),encoding='utf-8')
    from app.main import export_csv
    (d/'jobs.csv').write_bytes(export_csv().body)
if __name__=='__main__': main()
