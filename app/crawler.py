"""Nationwide, bounded discovery and daily rechecking. All run metrics are persisted."""
from __future__ import annotations
import contextlib, heapq, json, os, re, threading, time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from . import store
from .config import DATA_DIR, WORKERS, PAGES_PER_SOURCE, JST, DAILY_HOUR_JST, now_iso
from .extract import (canonical_url, read_document, classify, extract_job, content_fingerprint,
                      link_priority, HUB, HIRING, FIELD)
from .fetch import SafeFetcher, FetchError, RobotsBlocked
from .registry import refresh_registry

# External recruitment providers are followed only from official pages and only for the same employer.
EXTERNAL_HOSTS={'public-connect.jp','www.public-connect.jp','employment.en-japan.com',
                'www.enjapan.com','en-ambi.com','mid-tenshoku.com','www.saiyou-shiken.jp',
                'www.iju-join.jp','smout.jp'}


@contextlib.contextmanager
def process_lock():
    path=DATA_DIR/'crawl.lock'
    f=open(path,'a+b')
    try:
        if os.name=='nt':
            import msvcrt
            f.seek(0); f.write(b'0'); f.flush(); f.seek(0)
            try: msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            except OSError: raise RuntimeError('別の巡回処理が実行中です')
        else:
            import fcntl
            try: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: raise RuntimeError('別の巡回処理が実行中です')
        yield
    finally:
        f.close()


def allowed_link(url: str, official_hosts: set[str], from_official: bool=False) -> bool:
    p=urlsplit(url)
    if p.scheme not in ('http','https') or not p.hostname: return False
    if re.search(r'\.(?:jpg|jpeg|png|gif|webp|mp4|zip|xlsx?|docx?|pptx?)(?:$|\?)',url,re.I): return False
    if p.hostname in official_hosts: return True
    return from_official and p.hostname in EXTERNAL_HOSTS


def crawl_source(source: dict, fetcher: SafeFetcher, page_limit: int=PAGES_PER_SOURCE) -> dict:
    sid=source['id']; started=time.monotonic(); queue=[]; sequence=0; queued=set(); seen=set()
    pages=0; found=0; errors=[]; reached_hub=False
    hosts={urlsplit(source['url']).hostname}
    current_job_urls=set()
    with store.connect() as c:
        known=c.execute('SELECT url FROM jobs WHERE source_id=?',(sid,)).fetchall()
        pending=c.execute('''SELECT url,title,priority,last_checked FROM pages WHERE source_id=?
          ORDER BY CASE WHEN last_checked IS NULL THEN 0 ELSE 1 END,last_checked ASC,priority DESC LIMIT 100''',(sid,)).fetchall()
    def push(url: str,priority: int,depth: int=0):
        nonlocal sequence
        url=canonical_url(url)
        if url in queued or url in seen or depth>4 or len(queued)>600: return
        sequence+=1; queued.add(url)
        heapq.heappush(queue,(-priority,sequence,url,depth))
    for row in known:
        current_job_urls.add(row['url']); push(row['url'],1000)
        hosts.add(urlsplit(row['url']).hostname)
    for url in json.loads(source.get('entrypoints','[]')):
        push(url,900); hosts.add(urlsplit(url).hostname)
    push(source['url'],800)
    for row in pending:
        push(row['url'],min(200,row['priority']+30))
    while queue and pages<page_limit and time.monotonic()-started<150:
        _,_,url,depth=heapq.heappop(queue)
        if url in seen: continue
        seen.add(url)
        try:
            response=fetcher.get(url); pages+=1
            is_official=urlsplit(url).hostname in hosts
            if is_official and urlsplit(response.url).hostname not in EXTERNAL_HOSTS:
                hosts.add(urlsplit(response.url).hostname)
            doc=read_document(response.body,response.url,response.content_type)
            if not is_official and source['name'] not in doc['text']:
                continue
            if HUB.search(doc['title']): reached_hub=True
            candidate=classify(doc)
            if candidate.get('candidate'): reached_hub=True
            # Attach role-specific recruitment PDFs to their HTML parent, not application forms.
            if candidate.get('candidate') and not candidate.get('multiple_roles') and not doc.get('is_pdf'):
                attachments=[x for x in doc['links'] if '.pdf' in urlsplit(x['url']).path.lower()
                             and re.search(r'募集案内|募集要項|選考案内|試験案内',x['label'])]
                for a in attachments[:2]:
                    if pages>=page_limit or a['url'] in seen: break
                    if not allowed_link(a['url'],hosts): continue
                    try:
                        pdf=fetcher.get(a['url']); pages+=1; seen.add(a['url'])
                        pd=read_document(pdf.body,pdf.url,pdf.content_type)
                        doc['text']+='\n\n募集要項PDF\n'+pd['text']
                        doc.setdefault('attachment_urls',[]).append(a['url'])
                    except (FetchError,ValueError,RuntimeError) as e:
                        errors.append({'url':a['url'],'error':str(e)[:200]})
            fingerprint=content_fingerprint(doc)
            with store.connect() as c:
                c.execute('''INSERT INTO pages(url,source_id,title,content_hash,text_excerpt,last_checked,state,priority)
                  VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET title=excluded.title,
                  content_hash=excluded.content_hash,text_excerpt=excluded.text_excerpt,
                  last_checked=excluded.last_checked,state=excluded.state,error=NULL''',
                  (url,sid,doc['title'],fingerprint,doc['text'][:16000],now_iso(),'ok',link_priority(doc['title'],url)))
            if candidate.get('candidate'):
                payload,publish=extract_job(doc,source,url)
                payload['attachment_urls']=doc.get('attachment_urls',[])
                payload['entity_type']=source.get('kind','municipality')
                payload['verified_at']=now_iso()
                payload['research_method']='automatic_crawl'
                payload['snapshot_only']=False
                if source.get('kind')=='national':
                    payload['prefecture']='勤務地未抽出'
                    payload['region']='国の機関'
                created,_=store.save_candidate(sid,url,payload,fingerprint,publish)
                found+=int(created)
            elif url in current_job_urls:
                # A disappeared/moved role must not silently retain a fresh 'open' badge.
                store.mark_job_error(url,'ページは取得できましたが、募集本文を再確認できません。')
            for link in doc['links']:
                score=link_priority(link['label'],link['url'])
                if score<12 or not allowed_link(link['url'],hosts,is_official): continue
                if not is_official and urlsplit(link['url']).hostname not in hosts: continue
                push(link['url'],score,depth+1)
                with store.connect() as c:
                    c.execute('''INSERT OR IGNORE INTO pages(url,source_id,title,state,priority)
                      VALUES(?,?,?,?,?)''',(link['url'],sid,link['label'],'pending',score))
        except Exception as e:
            msg=str(e)[:300]; errors.append({'url':url,'error':msg})
            store.mark_job_error(url,msg)
            with store.connect() as c:
                c.execute('''INSERT INTO pages(url,source_id,last_checked,state,error)
                  VALUES(?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET last_checked=excluded.last_checked,
                  state=excluded.state,error=excluded.error''',(url,sid,now_iso(),'error',msg))
    state='ok' if pages and not errors and reached_hub else 'partial' if pages else 'error'
    note=json.dumps(errors[:6],ensure_ascii=False) if errors else '採用ページへの到達は未確認' if not reached_hub else None
    with store.connect() as c:
        c.execute('''UPDATE sources SET last_checked=?,last_success=COALESCE(?,last_success),
          state=?,error=?,pages_count=? WHERE id=?''',
          (now_iso(),now_iso() if pages else None,state,note,pages,sid))
    return {'pages':pages,'found':found,'error':int(state!='ok'),'state':state}


def run_crawl(prefecture: str|None=None, limit: int|None=None, refresh: bool=False,
              page_limit: int=PAGES_PER_SOURCE, fetcher: SafeFetcher|None=None, max_seconds: int|None=None) -> dict:
    f=fetcher or SafeFetcher()
    with process_lock():
        run_id=store.uid(now_iso()+str(time.monotonic()))
        with store.connect() as c:
            c.execute("UPDATE runs SET state='interrupted',finished_at=?,message='前回プロセスの中断を検知' WHERE state='running'",(now_iso(),))
            c.execute('INSERT INTO runs(id,started_at,state,message) VALUES(?,?,?,?)',
                      (run_id,now_iso(),'running','全国名簿・公式採用ページを確認中'))
        try:
            registry=store.setting('registry',{})
            national_registry=store.setting('national_registry',{})
            age=99
            if registry.get('checked_at'):
                age=(datetime.now(JST)-datetime.fromisoformat(registry['checked_at'])).days
            if refresh or registry.get('state')!='ok' or national_registry.get('state')!='ok' or age>=7:
                registry=refresh_registry(f)
            rows=[x for x in store.sources() if x['enabled'] and (not prefecture or x['pref']==prefecture)]
            rows.sort(key=lambda x:(x.get('last_checked') or '',x['name']))
            if limit: rows=rows[:limit]
            with store.connect() as c:
                c.execute('UPDATE runs SET total=? WHERE id=?',(len(rows),run_id))
            metrics={'done':0,'pages':0,'found':0,'errors':0}
            deadline=time.monotonic()+max_seconds if max_seconds is not None else None
            pending_rows=iter(rows)
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                tasks={}
                def submit_next():
                    if deadline is not None and time.monotonic()>=deadline: return
                    source=next(pending_rows,None)
                    if source is not None: tasks[pool.submit(crawl_source,source,f,page_limit)]=source
                for _ in range(WORKERS): submit_next()
                while tasks:
                    finished,_=wait(tasks,return_when=FIRST_COMPLETED)
                    for future in finished:
                        tasks.pop(future)
                        try:
                            r=future.result()
                        except Exception:
                            r={'pages':0,'found':0,'error':1}
                        metrics['done']+=1; metrics['pages']+=r['pages']; metrics['found']+=r['found']; metrics['errors']+=r['error']
                        with store.connect() as c:
                            c.execute('UPDATE runs SET done=?,pages=?,found=?,errors=?,message=? WHERE id=?',
                              (*metrics.values(),f"{metrics['done']}/{len(rows)} 収集先を確認",run_id))
                        submit_next()
            final_state='partial' if metrics['done']<len(rows) or metrics['errors'] or registry.get('state')!='ok' or store.setting('national_registry',{}).get('state')!='ok' else 'completed'
            with store.connect() as c:
                c.execute('UPDATE runs SET finished_at=?,state=?,message=? WHERE id=?',
                          (now_iso(),final_state,('実行枠の上限で一部を繰越。次回は未巡回・古い確認先を優先。' if metrics['done']<len(rows) else '巡回終了。未取得・要確認は収集状況画面をご確認ください。'),run_id))
            if not prefecture and not limit:
                store.set_setting('last_daily_date',datetime.now(JST).date().isoformat())
            return {'id':run_id,'state':final_state,**metrics}
        except Exception as e:
            with store.connect() as c:
                c.execute('UPDATE runs SET finished_at=?,state=?,message=? WHERE id=?',
                          (now_iso(),'failed',str(e)[:500],run_id))
            raise


class Runner:
    def __init__(self):
        self.thread: threading.Thread|None=None
        self.lock=threading.Lock()
        self.stop=threading.Event()
    def start(self,**kwargs) -> bool:
        with self.lock:
            if self.thread and self.thread.is_alive(): return False
            def target():
                import logging
                try: run_crawl(**kwargs)
                except Exception: logging.exception('crawl failed')
            self.thread=threading.Thread(target=target,name='civic-pr-crawler',daemon=True)
            self.thread.start(); return True
    def schedule(self):
        while not self.stop.wait(30):
            now=datetime.now(JST)
            today=now.date().isoformat()
            if (now.hour>=DAILY_HOUR_JST and store.setting('last_daily_date')!=today
                    and store.setting('last_scheduled_attempt_date')!=today):
                if self.start():
                    store.set_setting('last_scheduled_attempt_date',today)

runner=Runner()