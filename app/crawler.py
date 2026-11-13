"""Bounded collection with a persistent frontier and per-institution evidence."""
from __future__ import annotations
import contextlib, heapq, json, os, re, threading, time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from . import store
from .config import DATA_DIR, WORKERS, PAGES_PER_SOURCE, JST, DAILY_HOUR_JST, now_iso
from .extract import (canonical_url, read_document, classify, extract_job,
                      content_fingerprint, link_priority, HUB)
from .fetch import SafeFetcher, FetchError, RobotsBlocked
from .audit import error_code, finish_source, parse_time
from .registry import refresh_registry

EXTERNAL_HOSTS={'public-connect.jp','www.public-connect.jp','employment.en-japan.com',
                'www.enjapan.com','en-ambi.com','mid-tenshoku.com','www.saiyou-shiken.jp',
                'www.iju-join.jp','smout.jp'}

@contextlib.contextmanager
def process_lock():
    f=open(DATA_DIR/'crawl.lock','a+b')
    try:
        if os.name=='nt':
            import msvcrt
            f.seek(0); f.write(b'0'); f.flush(); f.seek(0)
            try: msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            except OSError: raise RuntimeError('Another crawl is already running.')
        else:
            import fcntl
            try: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: raise RuntimeError('Another crawl is already running.')
        yield
    finally:
        f.close()


def official_host(host, official_hosts):
    if not host:
        return False
    for item in official_hosts:
        if not item:
            continue
        base=item.removeprefix('www.')
        if host.removeprefix('www.')==base or host.endswith('.'+base):
            return True
    return False


def allowed_link(url: str, official_hosts: set[str], from_official: bool=False) -> bool:
    p=urlsplit(url)
    if p.scheme not in ('http','https') or not p.hostname: return False
    if re.search(r'\.(?:jpg|jpeg|png|gif|webp|mp4|zip|xlsx?|docx?|pptx?)(?:$|\?)',url,re.I): return False
    return official_host(p.hostname,official_hosts) or (from_official and p.hostname in EXTERNAL_HOSTS)


def crawl_source(source: dict, fetcher: SafeFetcher, page_limit: int=PAGES_PER_SOURCE,
                 run_id: str|None=None, absolute_deadline: float|None=None) -> dict:
    sid=source['id']; started=time.monotonic(); started_at=now_iso()
    source_deadline=started+max(10,min(600,int(os.getenv('SOURCE_BUDGET_SECONDS','150'))))
    if absolute_deadline is not None: source_deadline=min(source_deadline,absolute_deadline)
    queue=[]; queued=set(); seen=set(); sequence=0; skipped_links=0
    result=dict(pages=0,parsed=0,attempts=0,found=0,updated=0,candidates=0,hubs=0,
                published_added=0,review_added=0,errors=[],hub_url=None)
    hosts={urlsplit(source['url']).hostname}
    entries=json.loads(source.get('entrypoints') or '[]')
    with store.connect() as c:
        known=[dict(r) for r in c.execute('SELECT url,payload,last_checked FROM jobs WHERE source_id=?',(sid,))]
        previous={r['url']:dict(r) for r in c.execute('SELECT * FROM pages WHERE source_id=?',(sid,))}
    for job in known:
        previous.setdefault(job['url'],{'url':job['url'],'last_checked':job['last_checked'],
            'priority':160,'depth':0,'state':'known','page_kind':'page','title':''})
    current_job_urls={x['url'] for x in known}
    # Trusted manual entrypoints are part of the registry, not text extracted from a page.
    for entry in entries:
        if urlsplit(entry).hostname not in EXTERNAL_HOSTS:
            hosts.add(urlsplit(entry).hostname)
    now=datetime.now(JST)
    def due_priority(url,score):
        old=previous.get(url)
        if not old or not old.get('last_checked'):
            return 100000+min(500,max(0,score))
        age=(now-(parse_time(old.get('last_checked')) or now)).total_seconds()/86400
        return (75000 if old.get('state')=='error' else 10000)+min(20000,int(max(age,0)*500))+min(500,max(0,score))
    def push(url,score,depth=0,boost=None,label=''):
        nonlocal sequence,skipped_links
        url=canonical_url(url)
        if url in queued or url in seen: return
        if depth>6 or len(queued)>=1200:
            skipped_links+=1
            return
        sequence+=1; queued.add(url)
        with store.connect() as c:
            c.execute('''INSERT OR IGNORE INTO pages
              (url,source_id,title,state,priority,discovered_at,depth)
              VALUES(?,?,?,'pending',?,?,?)''',(url,sid,label,score,now_iso(),depth))
        p=boost if boost is not None else due_priority(url,score)
        heapq.heappush(queue,(-p,sequence,url,depth))
    # Fixed refresh anchors are bounded, leaving capacity for the unexplored frontier.
    push(source['url'],80,boost=300000,label=source['name'])
    hubs=sorted((p for p in previous.values() if p.get('page_kind')=='recruitment'),
                key=lambda p:p.get('last_checked') or '')
    anchor_urls=list(dict.fromkeys([p['url'] for p in hubs]+entries))
    anchor_budget=1 if page_limit<8 else 2
    for i,url in enumerate(anchor_urls):
        push(url,100,boost=250000 if i<anchor_budget else None)
    active=[]
    for j in known:
        payload=json.loads(j['payload'])
        last=parse_time(j['last_checked'])
        if store.deadline_status(payload)=='closed' and last and (now-last).days<7:
            continue
        active.append(j)
    active.sort(key=lambda j:j.get('last_checked') or '')
    for i,j in enumerate(active):
        # At most one third of the attempt budget has priority over discovery.
        push(j['url'],160,boost=180000 if i<max(1,page_limit//3) else None)
    # Unvisited and failed links survive the batch; their previous depth is retained.
    for p in sorted(previous.values(),key=lambda p:(p.get('last_checked') is not None,
                                                    p.get('last_checked') or '',-p['priority'])):
        if p['url'] in current_job_urls and p['url'] not in {j['url'] for j in active}:
            continue
        push(p['url'],p['priority'],p.get('depth') or 0,label=p.get('title') or '')

    def fail(url,exc):
        item={'url':url,'code':error_code(exc),'error':str(exc)[:320]}
        result['errors'].append(item)
        store.mark_job_error(url,item['error'])
        with store.connect() as c:
            c.execute('''INSERT INTO pages(url,source_id,last_checked,state,error,discovered_at)
              VALUES(?,?,?,'error',?,?) ON CONFLICT(source_id,url) DO UPDATE SET
              last_checked=excluded.last_checked,state='error',error=excluded.error''',
              (url,sid,now_iso(),item['error'],now_iso()))
    def save_page(url,doc,depth,kind):
        with store.connect() as c:
            c.execute('''INSERT INTO pages(url,source_id,title,content_hash,text_excerpt,
              last_checked,state,priority,discovered_at,depth,page_kind)
              VALUES(?,?,?,?,?,?,'ok',?,?,?,?) ON CONFLICT(source_id,url) DO UPDATE SET
              title=excluded.title,content_hash=excluded.content_hash,text_excerpt=excluded.text_excerpt,
              last_checked=excluded.last_checked,state='ok',error=NULL,
              priority=excluded.priority,page_kind=excluded.page_kind''',
              (url,sid,doc['title'],content_fingerprint(doc),doc['text'][:2400],now_iso(),
               link_priority(doc['title'],url),now_iso(),depth,kind))
    def fetch_doc(url):
        result['attempts']+=1
        response=fetcher.get(url); result['pages']+=1
        doc=read_document(response.body,response.url,response.content_type)
        result['parsed']+=1
        return response,doc

    while queue and result['attempts']<page_limit and time.monotonic()<source_deadline:
        _,_,url,depth=heapq.heappop(queue)
        if url in seen: continue
        seen.add(url)
        try:
            response,doc=fetch_doc(url)
            is_official=official_host(urlsplit(url).hostname,hosts)
            if is_official and urlsplit(response.url).hostname not in EXTERNAL_HOSTS:
                hosts.add(urlsplit(response.url).hostname)
            if not is_official and source['name'] not in doc['text']:
                save_page(url,doc,depth,'external_employer_unconfirmed')
                if url in current_job_urls:
                    store.mark_job_error(url,'External page employer could not be confirmed.')
                continue
            candidate=classify(doc)
            reached=bool(HUB.search(doc['title']) or candidate.get('candidate')) and not doc.get('is_feed')
            if reached:
                result['hubs']+=1;result['hub_url']=response.url
            if candidate.get('candidate') and not candidate.get('multiple_roles') and not doc.get('is_pdf'):
                attachments=[x for x in doc['links'] if '.pdf' in urlsplit(x['url']).path.lower()
                             and re.search(r'\u52df\u96c6\u6848\u5185|\u52df\u96c6\u8981\u9805|\u9078\u8003\u6848\u5185|\u8a66\u9a13\u6848\u5185',x['label'])]
                for a in attachments[:2]:
                    if a['url'] in seen or not allowed_link(a['url'],hosts): continue
                    if result['attempts']>=page_limit or time.monotonic()>=source_deadline: break
                    seen.add(a['url'])
                    try:
                        _,pd=fetch_doc(a['url'])
                        save_page(a['url'],pd,depth+1,'attachment')
                        doc['text']+='\n\nRecruitment PDF\n'+pd['text']
                        doc.setdefault('attachment_urls',[]).append(a['url'])
                    except Exception as e:
                        fail(a['url'],e)
            save_page(url,doc,depth,'recruitment' if reached else 'feed' if doc.get('is_feed') else 'page')
            if candidate.get('candidate'):
                result['candidates']+=1
                payload,publish=extract_job(doc,source,url)
                payload.update(attachment_urls=doc.get('attachment_urls',[]),
                    entity_type=source.get('kind','municipality'),verified_at=now_iso(),
                    research_method='automatic_crawl',snapshot_only=False)
                if source.get('kind')=='national':
                    payload['prefecture']='\u52e4\u52d9\u5730\u672a\u62bd\u51fa'
                    payload['region']='\u56fd\u306e\u6a5f\u95a2'
                created,changed=store.save_candidate(sid,url,payload,content_fingerprint(doc),publish)
                result['found']+=int(created);result['updated']+=int(changed)
                result['published_added']+=int(created and publish)
                result['review_added']+=int(created and not publish)
            elif url in current_job_urls:
                store.mark_job_error(url,'Page fetched, but the recruitment body could not be reconfirmed.')
            for link in doc['links']:
                score=link_priority(link['label'],link['url'])
                if score<12 or not allowed_link(link['url'],hosts,is_official): continue
                if not is_official and not official_host(urlsplit(link['url']).hostname,hosts): continue
                push(link['url'],score,depth+1,label=link['label'])
        except Exception as e:
            fail(url,e)
    result['remaining']=len({x[2] for x in queue if x[2] not in seen})
    result['skipped_links']=skipped_links
    result['finish_reason']=('time_budget' if result['remaining'] and time.monotonic()>=source_deadline else
       'page_budget' if result['remaining'] and result['attempts']>=page_limit else
       'frontier_limit' if skipped_links else 'queue_exhausted')
    result['state']=('blocked' if not result['parsed'] and result['errors'] and
                       all(e['code']=='robots' for e in result['errors']) else
                     'error' if not result['parsed'] else
                     'ok' if result['hubs'] and not result['errors'] and not result['remaining'] and not skipped_links else 'partial')
    result['elapsed']=time.monotonic()-started
    result['error']=int(result['state']!='ok')
    finish_source(source,result,run_id,started_at)
    return result


def run_crawl(prefecture: str|None=None, limit: int|None=None, refresh: bool=False,
              page_limit: int=PAGES_PER_SOURCE, fetcher: SafeFetcher|None=None,
              max_seconds: int|None=None) -> dict:
    f=fetcher or SafeFetcher()
    with process_lock():
        run_id=store.uid(now_iso()+str(time.monotonic()))
        deadline=time.monotonic()+max_seconds if max_seconds is not None else None
        with store.connect() as c:
            c.execute("UPDATE runs SET state='interrupted',finished_at=?,message='Previous process interrupted; unfinished work remains queued.' WHERE state='running'",(now_iso(),))
            c.execute('INSERT INTO runs(id,started_at,state,message,audit_version) VALUES(?,?,?,?,3)',
                      (run_id,now_iso(),'running','Checking official registries and recruitment pages.'))
        try:
            registry=store.setting('registry',{});national=store.setting('national_registry',{})
            checked=parse_time(registry.get('checked_at'))
            age=(datetime.now(JST)-checked).days if checked else 99
            if (deadline is None or time.monotonic()<deadline) and (refresh or registry.get('state')!='ok' or national.get('state')!='ok' or age>=7):
                registry=refresh_registry(f)
            rows=[s for s in store.sources() if s['enabled'] and (not prefecture or s['pref']==prefecture)]
            # First coverage, then the least recently attempted institutions; no alphabetical starvation.
            rows.sort(key=lambda s:(s.get('last_checked') or '',s['name']))
            if limit: rows=rows[:limit]
            with store.connect() as c: c.execute('UPDATE runs SET total=? WHERE id=?',(len(rows),run_id))
            totals=dict(done=0,pages=0,found=0,errors=0,attempted_pages=0,parsed_pages=0,
                failed_pages=0,hub_sources=0,site_only_sources=0,failed_sources=0,
                partial_sources=0,budget_sources=0,updated=0,published_added=0,review_added=0)
            remaining=iter(rows)
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                tasks={}
                def submit_next():
                    if deadline is not None and time.monotonic()>=deadline: return
                    s=next(remaining,None)
                    if s is not None:
                        tasks[pool.submit(crawl_source,s,f,page_limit,run_id,deadline)]=s
                for _ in range(WORKERS): submit_next()
                while tasks:
                    finished,_=wait(tasks,return_when=FIRST_COMPLETED)
                    for future in finished:
                        s=tasks.pop(future)
                        try:
                            r=future.result()
                        except Exception as e:
                            # Never silently lose which institution failed.
                            r=dict(pages=0,parsed=0,attempts=0,found=0,updated=0,candidates=0,hubs=0,
                                published_added=0,review_added=0,errors=[{'url':s['url'],'code':'worker_error','error':str(e)[:300]}],
                                hub_url=None,remaining=0,finish_reason='worker_error',elapsed=0,state='error',error=1)
                            finish_source(s,r,run_id,now_iso())
                        totals['done']+=1;totals['pages']+=r['pages'];totals['found']+=r['found'];totals['errors']+=r['error']
                        totals['attempted_pages']+=r['attempts'];totals['parsed_pages']+=r['parsed'];totals['failed_pages']+=len(r['errors'])
                        totals['hub_sources']+=int(r['hubs']>0)
                        totals['site_only_sources']+=int(r['parsed']>0 and not r['hubs'])
                        totals['failed_sources']+=int(not r['parsed'])
                        totals['partial_sources']+=int(r['state']=='partial')
                        totals['budget_sources']+=int(r['finish_reason'] in ('page_budget','time_budget','frontier_limit'))
                        for k in ('updated','published_added','review_added'): totals[k]+=r[k]
                        with store.connect() as c:
                            c.execute('UPDATE runs SET '+','.join(k+'=?' for k in totals)+',message=? WHERE id=?',
                                (*totals.values(),str(totals['done'])+'/'+str(len(rows))+' institutions processed; see detailed coverage.',run_id))
                        print(json.dumps({'event':'source_finished','source':s['name'],'prefecture':s['pref'],
                            'state':r['state'],'attempts':r['attempts'],'parsed':r['parsed'],
                            'recruitment_pages':r['hubs'],'remaining':r['remaining'],'new':r['found'],
                            'done':totals['done'],'total':len(rows)},ensure_ascii=False),flush=True)
                        submit_next()
            state='partial' if totals['done']<len(rows) or totals['errors'] or registry.get('state')!='ok' or store.setting('national_registry',{}).get('state')!='ok' else 'completed'
            deferred=max(0,len(rows)-totals['done'])
            with store.connect() as c:
                c.execute('UPDATE runs SET finished_at=?,state=?,deferred_sources=?,message=? WHERE id=?',
                    (now_iso(),state,deferred,'Batch finished. Unvisited or unresolved institutions are not zero-job results.',run_id))
                cutoff=(datetime.now(JST)-timedelta(days=90)).isoformat()
                c.execute('DELETE FROM source_runs WHERE finished_at<?',(cutoff,))
            if not prefecture and not limit:
                store.set_setting('last_daily_date',datetime.now(JST).date().isoformat())
            return {'id':run_id,'state':state,'deferred_sources':deferred,**totals}
        except Exception as e:
            with store.connect() as c:
                c.execute('UPDATE runs SET finished_at=?,state=?,message=? WHERE id=?',
                    (now_iso(),'failed',str(e)[:500],run_id))
            raise


class Runner:
    def __init__(self):
        self.thread: threading.Thread|None=None
        self.lock=threading.Lock();self.stop=threading.Event()
    def start(self,**kwargs) -> bool:
        with self.lock:
            if self.thread and self.thread.is_alive(): return False
            def target():
                import logging
                try: run_crawl(**kwargs)
                except Exception: logging.exception('crawl failed')
            self.thread=threading.Thread(target=target,name='civic-pr-crawler',daemon=True)
            self.thread.start();return True
    def schedule(self):
        while not self.stop.wait(30):
            now=datetime.now(JST);today=now.date().isoformat()
            if (now.hour>=DAILY_HOUR_JST and store.setting('last_daily_date')!=today
                    and store.setting('last_scheduled_attempt_date')!=today):
                if self.start(): store.set_setting('last_scheduled_attempt_date',today)

runner=Runner()
