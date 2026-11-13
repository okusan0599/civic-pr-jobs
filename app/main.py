"""HTTP API and application entry point. Run a single worker with persistent DATA_DIR."""
from __future__ import annotations
import csv, io, json, secrets, threading
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit
from fastapi import FastAPI, HTTPException, Depends, Header, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from . import store
from .config import (ROOT, VERSION, ADMIN_TOKEN, SCHEDULER_ENABLED, CRAWL_ON_START,
                     DAILY_HOUR_JST, PREF_NAMES, now_iso)
from .crawler import runner
from .fetch import validate_public_url, FetchError


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init_db()
    if CRAWL_ON_START: runner.start()
    scheduler=None
    if SCHEDULER_ENABLED:
        scheduler=threading.Thread(target=runner.schedule,name='daily-scheduler',daemon=True)
        scheduler.start()
    yield
    runner.stop.set()


app=FastAPI(title='CIVIC PR JOBS',version=VERSION,lifespan=lifespan,docs_url='/api/docs')

@app.middleware('http')
async def security_headers(request, call_next):
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='no-referrer'
    response.headers['X-Frame-Options']='DENY'
    response.headers['Permissions-Policy']='camera=(), microphone=(), geolocation=()'
    # Bundled single-file UI intentionally uses inline scripts and styles, but no third-party JS.
    response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control']='no-store'
    return response


def admin(authorization: str|None=Header(default=None)):
    if not ADMIN_TOKEN:
        raise HTTPException(503,'管理トークンが未設定です。ADMIN_TOKENを設定して再起動してください。')
    expected='Bearer '+ADMIN_TOKEN
    if not authorization or not secrets.compare_digest(authorization,expected):
        raise HTTPException(401,'管理トークンが一致しません。')


@app.get('/',response_class=HTMLResponse)
def index():
    return (ROOT/'web'/'index.html').read_text(encoding='utf-8')


@app.get('/healthz')
def health():
    with store.connect() as c: c.execute('SELECT 1')
    return {'status':'ok','version':VERSION}


@app.get('/api/meta')
def meta():
    m=store.metadata()
    m.update(scheduler_enabled=SCHEDULER_ENABLED,daily_hour_jst=DAILY_HOUR_JST,
             crawl_running=bool(runner.thread and runner.thread.is_alive()),
             admin_configured=bool(ADMIN_TOKEN))
    return m


def filter_jobs(rows: list[dict], q: str='', region: str='', prefecture: str='',
                status: str='', category: str='', tag: str='', min_salary: int=0, entity_type: str='') -> list[dict]:
    words=q.casefold().split()
    def keep(j):
        text=json.dumps(j,ensure_ascii=False).casefold()
        return (all(w in text for w in words) and (not region or j.get('region')==region)
          and (not prefecture or j.get('prefecture')==prefecture)
          and (not status or j.get('status') in status.split(','))
          and (not category or j.get('category')==category)
          and (not entity_type or (entity_type=='government' and j.get('entity_type','municipality') in ('national','prefecture','municipality','ward')) or j.get('entity_type','municipality')==entity_type)
          and (not tag or tag in j.get('tags',[]))
          and (not min_salary or (j.get('salary_annual_min') or 0)>=min_salary))
    return [j for j in rows if keep(j)]


@app.get('/api/jobs')
def get_jobs(q: str='',region: str='',prefecture: str='',status: str='',category: str='',
             tag: str='',entity_type: str='',min_salary: int=Query(0,ge=0),offset: int=Query(0,ge=0),limit: int=Query(10000,ge=1,le=10000)):
    rows=filter_jobs(store.jobs(),q,region,prefecture,status,category,tag,min_salary,entity_type)
    return {'total':len(rows),'items':rows[offset:offset+limit]}


@app.get('/api/sources')
def get_sources():
    return {'items':store.sources()}


@app.get('/api/research-audit')
def research_audit():
    return store.metadata()['research_audit']


@app.get('/api/history')
def get_history(job_id: str|None=None):
    # Proposed extraction is reserved for authenticated review.
    rows=store.history(job_id)
    for x in rows: x.pop('proposed_payload',None)
    return {'items':rows}


@app.get('/api/admin/reviews',dependencies=[Depends(admin)])
def reviews():
    rows=[j for j in store.jobs(True) if j['visibility']=='review' or j.get('source_changed')]
    for j in rows:
        proposals=[x for x in store.history(j['id']) if x.get('proposed_payload')]
        j['proposed_payload']=json.loads(proposals[0]['proposed_payload']) if proposals else None
    return {'items':rows}


class CrawlRequest(BaseModel):
    prefecture: str|None=None
    limit: int|None=Field(None,ge=1,le=2500)
    refresh_registry: bool=False


@app.post('/api/admin/crawl',dependencies=[Depends(admin)])
def crawl(req: CrawlRequest):
    if req.prefecture and req.prefecture not in PREF_NAMES+['国の機関']:
        raise HTTPException(400,'都道府県が不正です')
    if not runner.start(prefecture=req.prefecture,limit=req.limit,refresh=req.refresh_registry):
        raise HTTPException(409,'すでに巡回中です。収集状況から進捗を確認してください。')
    return {'started':True}


class SourceRequest(BaseModel):
    name: str=Field(min_length=2,max_length=50)
    prefecture: str
    official_url: str=Field(max_length=2000)
    recruitment_url: str|None=Field(None,max_length=2000)
    kind: Literal['national','prefecture','municipality','ward','public_body']='municipality'


@app.post('/api/admin/sources',dependencies=[Depends(admin)])
def add_source(req: SourceRequest):
    if req.prefecture not in PREF_NAMES+['国の機関']: raise HTTPException(400,'都道府県が不正です')
    try:
        validate_public_url(req.official_url)
        if req.recruitment_url: validate_public_url(req.recruitment_url)
    except FetchError as e:
        raise HTTPException(400,str(e))
    sid=store.add_source(req.name,'国の機関' if req.kind=='national' else req.prefecture,req.official_url,kind=req.kind,provenance='管理者による公式URL登録',
                         entrypoints=[req.recruitment_url] if req.recruitment_url else [])
    return {'id':sid}


class ReviewRequest(BaseModel):
    action: Literal['publish','hide']='publish'
    payload: dict|None=None


@app.post('/api/admin/review/{job_id}',dependencies=[Depends(admin)])
def review(job_id: str,req: ReviewRequest):
    with store.connect() as c:
        row=c.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if not row: raise HTTPException(404,'案件が見つかりません')
        payload=req.payload or json.loads(row['payload'])
        for key in ('source_url','municipality_url'):
            if payload.get(key) and urlsplit(payload[key]).scheme not in ('http','https'):
                raise HTTPException(400,'URL形式が不正です')
        for key in ('opens_at','deadline_at'):
            if payload.get(key):
                try:
                    dt=datetime.fromisoformat(payload[key])
                    if not dt.tzinfo: raise ValueError('timezone required')
                except (ValueError,TypeError): raise HTTPException(400,f'{key}はタイムゾーン付き日時が必要です')
        annual=payload.get('salary_annual_min')
        if annual is not None and (not isinstance(annual,(float,int)) or annual<0 or annual>1000000000):
            raise HTTPException(400,'年収値が不正です')
        # Identity and official URL are immutable through the review editor.
        payload.update(id=job_id,source_id=row['source_id'],source_url=row['url'],source_changed=False,
                       snapshot_only=False,verified_at=now_iso())
        if len(store.dumps(payload))>80000: raise HTTPException(413,'本文が長すぎます')
        c.execute("UPDATE jobs SET payload=?,visibility=?,verification=?,source_state='ok',updated_at=?,last_success=? WHERE id=?",
                  (store.dumps(payload),'published' if req.action=='publish' else 'hidden','manual',now_iso(),now_iso(),job_id))
        c.execute('INSERT INTO changes(job_id,at,kind,summary) VALUES(?,?,?,?)',
                  (job_id,now_iso(),'reviewed','管理者が条件を確認して公開' if req.action=='publish' else '管理者が非表示に変更'))
    return {'ok':True}


def csv_cell(value):
    s=str(value if value is not None else '')
    return "'"+s if s.lstrip().startswith(('=','+','-','@','\t','\r')) else s


@app.get('/api/export.csv')
def export_csv():
    stream=io.StringIO(); w=csv.writer(stream)
    w.writerow(['自治体','都道府県','地域','募集職名','受付状況','採用方針','担当領域','給与','公表年収下限','任期','応募期限','原典URL','確認日時'])
    for j in store.jobs():
        w.writerow([csv_cell(v) for v in [j.get('municipality'),j.get('prefecture'),j.get('region'),j.get('title'),
          j.get('status'),j.get('hiring_policy'),'/'.join(j.get('tags',[])),j.get('salary_text'),j.get('salary_annual_min'),
          j.get('term_text'),j.get('deadline_at'),j.get('source_url'),j.get('checked_at')]])
    return Response('\ufeff'+stream.getvalue(),media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition':'attachment; filename="civic-pr-jobs.csv"'})