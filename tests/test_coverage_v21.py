"""Regression tests; every municipality/URL in this file is an offline fixture."""
import json
from datetime import datetime, timedelta
import pytest
from app import store
from app.config import now_iso, JST
from app.crawler import crawl_source, run_crawl, allowed_link
from app.fetch import FetchError, Response, RobotsBlocked
from app.extract import read_document, link_priority
from app.audit import error_code, metrics, source_run_history, write_ci_summary

TITLE='\u5e83\u5831\u5c02\u9580\u8077\u54e1\u63a1\u7528\u52df\u96c6'
HUB='\u8077\u54e1\u63a1\u7528\u60c5\u5831'
ROLE='<h1>'+TITLE+'</h1><h2>\u8077\u52d9\u5185\u5bb9</h2><p>\u5e83\u5831\u6226\u7565\u3068SNS\u3092\u62c5\u5f53\u3059\u308b\u5c02\u9580\u8077\u306e\u52df\u96c6\u3067\u3059\u3002</p><h2>\u5fdc\u52df\u8cc7\u683c</h2><p>\u6c11\u9593\u3067\u5e83\u5831\u7d4c\u9a135\u5e74\u4ee5\u4e0a</p>'

@pytest.fixture(autouse=True)
def db(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DB_PATH',tmp_path/'coverage.sqlite3')
    store.init_db(False)

@pytest.fixture
def source():
    sid=store.add_source('Fixture City','\u6771\u4eac\u90fd','https://city.example/')
    return next(s for s in store.sources() if s['id']==sid)

class Fake:
    def __init__(self,mapping): self.mapping=mapping;self.calls=[]
    def get(self,url):
        self.calls.append(url)
        value=self.mapping.get(url,FetchError('HTTP 404: fixture'))
        if isinstance(value,Exception): raise value
        if isinstance(value,tuple): return Response(url,value[0].encode(),value[1])
        return Response(url,value.encode(),'text/html')

def test_top_page_success_is_not_recruitment_success(source):
    crawl_source(source,Fake({source['url']:'<h1>Fixture City</h1>'}),6)
    m=metrics();s=store.sources()[0]
    assert m['attempted']==m['reachable']==1
    assert m['recruitment_recent']==0 and m['site_only']==1
    assert s['stage']=='site_only' and s['state']=='partial'

def test_http_200_unreadable_xml_not_counted_as_recruitment(source):
    r=crawl_source(source,Fake({source['url']:('<rss><invalid','application/xml')}),6)
    assert r['pages']==1 and r['parsed']==0
    assert store.sources()[0]['last_success'] is None
    assert metrics()['failed']==1

def test_sitemap_links_cross_discovery_threshold():
    assert link_priority('\u30b5\u30a4\u30c8\u30de\u30c3\u30d7','https://city.example/map')>=12
    assert link_priority('\u5e02\u653f\u60c5\u5831','https://city.example/cms1')>=12

def test_sitemap_to_hub_is_actually_followed(source):
    f=Fake({source['url']:'<h1>City</h1><a href="/sitemap">\u30b5\u30a4\u30c8\u30de\u30c3\u30d7</a>',
        source['url']+'sitemap':'<h1>Map</h1><a href="/recruit">'+HUB+'</a>',
        source['url']+'recruit':'<h1>'+HUB+'</h1>'})
    r=crawl_source(source,f,8)
    assert r['hubs']==1 and r['attempts']==3
    assert metrics()['recruitment_recent']==1

def test_rss_and_sitemap_xml_yield_real_links():
    doc=read_document(('<rss><channel><item><title>'+TITLE+'</title><link>https://city.example/recruit</link></item></channel></rss>').encode(),'https://city.example/feed','application/xml')
    assert doc['links'][0]['label']==TITLE and doc['is_feed']
    doc=read_document(b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://city.example/recruit</loc></url></urlset>','https://city.example/sitemap.xml','application/xml')
    assert len(doc['links'])==1

def test_xml_entities_are_not_expanded():
    with pytest.raises(ValueError):
        read_document(b'<!DOCTYPE rss [<!ENTITY x "large">]><rss>&x;</rss>','https://city.example/feed','application/xml')

def test_official_atom_feed_is_followed(source):
    atom='<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>'+TITLE+'</title><link href="/job"/></entry></feed>'
    f=Fake({source['url']:'<h1>City</h1><a href="/feed.xml">RSS</a>',source['url']+'feed.xml':(atom,'application/atom+xml'),source['url']+'job':ROLE})
    r=crawl_source(source,f,10)
    assert r['found']==1 and r['hubs']==1

def test_known_jobs_do_not_starve_discovery(source):
    known={}
    for i in range(12):
        url=source['url']+'known/'+str(i)
        store.save_candidate(source['id'],url,{'title':TITLE},'old'+str(i),True)
        known[url]=ROLE
    f=Fake({source['url']:'<h1>City</h1><a href="/new-role">'+TITLE+'</a>',source['url']+'new-role':ROLE,**known})
    r=crawl_source(source,f,6)
    assert source['url']+'new-role' in f.calls
    assert r['found']==1 and r['attempts']<=6

def test_pending_frontier_survives_page_limit_and_is_processed_next_time(source):
    root='<h1>City</h1>'+''.join('<a href="/role/'+str(i)+'">'+TITLE+'</a>' for i in range(9))
    mapping={source['url']:root,**{source['url']+'role/'+str(i):ROLE for i in range(9)}}
    first=crawl_source(source,Fake(mapping),3)
    assert first['finish_reason']=='page_budget' and store.sources()[0]['pending_pages']>0
    found_before=len(store.jobs(True))
    second=crawl_source(store.sources()[0],Fake(mapping),6)
    assert len(store.jobs(True))>found_before
    assert second['found']>0

def test_failures_are_bounded_by_attempts_not_only_successes(source):
    root='<h1>City</h1>'+''.join('<a href="/recruit/'+str(i)+'">'+HUB+'</a>' for i in range(20))
    f=Fake({source['url']:root})
    r=crawl_source(source,f,4)
    assert r['attempts']==4 and len(f.calls)==4 and len(r['errors'])==3
    assert store.sources()[0]['pending_pages']>0

def test_robots_failure_preserves_reason_and_url(source):
    r=crawl_source(source,Fake({source['url']:RobotsBlocked('robots.txt blocked')}),4)
    s=store.sources()[0]
    assert r['state']=='blocked' and s['stage']=='failed'
    assert s['error_details'][0]['url']==source['url']
    assert metrics()['error_breakdown']['robots']==1

def test_same_url_belongs_to_each_source_without_overwrite(source):
    second_id=store.add_source('Fixture Agency','\u56fd\u306e\u6a5f\u95a2',source['url'],kind='national')
    second=next(s for s in store.sources() if s['id']==second_id)
    f=Fake({source['url']:'<h1>'+HUB+'</h1>'})
    crawl_source(source,f,4);crawl_source(second,f,4)
    with store.connect() as c: rows=c.execute('SELECT source_id FROM pages WHERE url=?',(source['url'],)).fetchall()
    assert len(rows)==2 and len({r[0] for r in rows})==2

def test_migration_preserves_old_page_and_job_data(source):
    store.save_candidate(source['id'],source['url']+'job',{'title':TITLE},'old',True)
    with store.connect() as c:
        c.execute('DROP TABLE pages')
        c.execute('''CREATE TABLE pages(url TEXT PRIMARY KEY,source_id TEXT NOT NULL,title TEXT,content_hash TEXT,
        text_excerpt TEXT,last_checked TEXT,state TEXT,error TEXT,priority INTEGER NOT NULL DEFAULT 0)''')
        c.execute('INSERT INTO pages VALUES(?,?,?,?,?,?,?,?,?)',(source['url'],source['id'],'Original','abc','old body',now_iso(),'ok',None,50))
    store.init_db(False);store.init_db(False)
    with store.connect() as c:
        r=c.execute('SELECT * FROM pages').fetchone()
        assert r['title']=='Original' and r['content_hash']=='abc'
        assert {x[1] for x in c.execute('PRAGMA table_info(pages)') if x[5]}=={'source_id','url'}
    assert len(store.jobs())==1

def test_legacy_state_is_not_fabricated_new_audit(source):
    with store.connect() as c:c.execute("UPDATE sources SET state='ok',last_checked=?,last_success=? WHERE id=?",(now_iso(),now_iso(),source['id']))
    assert store.sources()[0]['stage']=='legacy'
    assert metrics()['recruitment_recent']==0

def test_last_hub_timestamp_survives_a_failure_but_latest_stage_does_not(source):
    crawl_source(source,Fake({source['url']:'<h1>'+HUB+'</h1>'}),4)
    stamp=store.sources()[0]['last_hub_success']
    crawl_source(store.sources()[0],Fake({}),4)
    assert store.sources()[0]['last_hub_success']==stamp
    assert store.sources()[0]['stage']=='failed'

def test_old_recruitment_evidence_is_not_recent(source):
    with store.connect() as c:c.execute('UPDATE sources SET last_hub_success=? WHERE id=?',((datetime.now(JST)-timedelta(days=8)).isoformat(),source['id']))
    assert metrics()['recruitment_recent']==0 and metrics()['recruitment_ever']==1

def test_every_prefecture_is_shown_even_with_no_registry():
    m=metrics()
    assert len(m['per_prefecture'])==48 and m['registered']==0
    assert not m['registry_complete']

def test_official_subdomain_allowed_but_provider_requires_provenance():
    assert allowed_link('https://recruit.city.example/jobs',{'www.city.example'})
    assert not allowed_link('https://public-connect.jp/job',{'city.example'})
    assert allowed_link('https://public-connect.jp/job',{'city.example'},True)
    assert not allowed_link('https://city.example.evil.test/',{'city.example'},True)

def test_source_run_log_and_real_batch_metrics(source):
    for key in ['registry','national_registry']:store.set_setting(key,{'state':'ok','checked_at':now_iso()})
    r=run_crawl(limit=1,page_limit=4,fetcher=Fake({source['url']:'<h1>'+HUB+'</h1>'}))
    logs=source_run_history()
    assert len(logs)==1 and logs[0]['source_id']==source['id'] and logs[0]['metrics']['hubs']==1
    assert r['hub_sources']==1 and r['failed_sources']==0 and r['done']==1

def test_worker_exception_is_persisted_not_silenced(source,monkeypatch):
    from app import crawler
    for key in ['registry','national_registry']:store.set_setting(key,{'state':'ok','checked_at':now_iso()})
    def fail(*args,**kwargs):raise RuntimeError('fixture worker failure')
    monkeypatch.setattr(crawler,'crawl_source',fail)
    r=crawler.run_crawl(limit=1,fetcher=Fake({}))
    assert r['failed_sources']==1
    assert store.sources()[0]['error_details'][0]['code']=='worker_error'
    assert source_run_history()[0]['state']=='error'

def test_batch_budget_defers_unvisited_institutions(source):
    for key in ['registry','national_registry']:store.set_setting(key,{'state':'ok','checked_at':now_iso()})
    r=run_crawl(fetcher=Fake({}),max_seconds=0)
    assert r['done']==0 and r['deferred_sources']==1 and metrics()['unvisited']==1

def test_ci_summary_records_partial_crawl_and_registry_failure(source,tmp_path,monkeypatch,capsys):
    path=tmp_path/'summary.md';monkeypatch.setenv('GITHUB_STEP_SUMMARY',str(path))
    write_ci_summary()
    text=path.read_text()
    assert 'Registered institutions' in text and 'NOT evidence' in text
    assert '::warning' in capsys.readouterr().out

@pytest.mark.parametrize('text,expected',[
    ('HTTP 404','not_found'),('HTTP 429','rate_limited'),('HTTP 503','server_error'),
    ('connection timed out','timeout'),('PDF text extraction','pdf_extraction'),
    ('robots.txt denied','robots'),('HTTP 403','access_denied'),('name resolution failed','dns_or_url_safety')])
def test_error_category(text,expected): assert error_code(FetchError(text))==expected
