"""Offline tests. Fixture municipalities are imaginary and never published as jobs."""
from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from app import store
from app.config import PREF_NAMES, now_iso
from app.extract import (read_document, extract_deadline, dates_in, classify, extract_job,
                         canonical_url, link_priority, content_fingerprint)
from app.fetch import validate_public_url, FetchError, Response, RobotsBlocked
from app.registry import registry_pages, municipalities, refresh_registry
from app.crawler import crawl_source, run_crawl, allowed_link

HTML='''<!doctype html><html><head><title>広報戦略専門職の採用募集</title></head><body>
<nav>広告掲載事業者を募集</nav><main><h1>広報戦略専門職の採用募集</h1>
<h2>職務内容</h2><p>広報戦略とシティプロモーション、SNSの運営を担当。</p>
<h2>応募資格</h2><p>民間企業等で広報の経験が5年以上ある人</p>
<h2>受付期間</h2><p>令和8年11月4日から令和8年11月24日17時まで</p>
<h2>給与</h2><p>月額360,000円、賞与は条件による</p>
<h2>任用期間</h2><p>令和9年4月1日から令和12年3月31日まで</p>
</main><footer>copyright</footer></body></html>'''

@pytest.fixture(autouse=True)
def temp_db(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DB_PATH',tmp_path/'test.sqlite3')
    store.init_db(False)

@pytest.fixture
def source():
    sid=store.add_source('試験市','東京都','https://municipal.example/')
    return next(x for x in store.sources() if x['id']==sid)

@pytest.mark.parametrize('text,expected',[
    ('受付期間\n令和8年11月4日から令和8年11月24日17時まで','2026-11-24T17:00:00+09:00'),
    ('応募期限：2026年12月2日 当日消印有効','2026-12-02T23:59:59+09:00'),
    ('応募締切：２０２６年１１月１５日２３時５９分まで','2026-11-15T23:59:00+09:00'),
    ('募集期間\n令和8年12月1日から12月27日まで','2026-12-27T23:59:59+09:00'),
    ('受付期間：2026/11/04から2026/11/24まで','2026-11-24T23:59:59+09:00'),
])
def test_deadline(text,expected):
    assert extract_deadline(text)['deadline_at']==expected

@pytest.mark.parametrize('text',[
    '更新日：2026年11月13日\n任用期間：2027年4月1日から2028年3月31日まで',
    '募集期間：11月4日から11月24日まで',
    '応募資格：経験10年以上',
    '応募締切：令和8年2月30日まで',
])
def test_unknown_deadline_not_invented(text):
    assert extract_deadline(text)['deadline_at'] is None


def test_conflicting_deadlines_stay_unknown():
    t='応募締切：2026年11月15日まで\n\n\n\n\n応募期限：2026年11月20日まで'
    assert extract_deadline(t)['deadline_at'] is None


def test_next_year_is_not_an_application_deadline():
    d=read_document(HTML.encode(),'https://municipal.example/job')
    assert extract_deadline(d['text'])['deadline_at'].startswith('2026-11-24')
    assert '広告掲載事業者' not in d['text']

@pytest.mark.parametrize('title',[
    '広報業務委託の公募型プロポーザルを実施',
    '広報事業の受託事業者募集',
    '広報紙の広告掲載募集',
    '広報専門職の採用試験 合格発表',
])
def test_procurement_and_results_excluded(title):
    assert not classify({'title':title,'text':'応募資格\n経験10年'})['candidate']


def test_role_tags_and_monthly_salary(source):
    doc=read_document(HTML.encode(),source['url'])
    job,publish=extract_job(doc,source,source['url']+'job')
    assert publish
    assert 'SNS・デジタル' in job['tags']
    assert job['salary_annual_min'] is None
    assert job['experience_years']==5
    assert job['deadline_at'].startswith('2026-11-24')


def test_multiple_roles_are_reviewed(source):
    doc=read_document(HTML.encode(),source['url'])
    doc['title']='一般任期付職員（広報戦略監・防災専門監）の募集'
    _,publish=extract_job(doc,source,source['url'])
    assert not publish

@pytest.mark.parametrize('url',[
    'http://127.0.0.1/', 'http://localhost/', 'http://169.254.169.254/latest/meta-data/',
    'http://10.0.0.1/', 'http://[::1]/', 'file:///etc/passwd',
    'https://a:b@example.com/', 'https://example.com:8888/'
])
def test_private_urls_and_credentials_rejected(url):
    with pytest.raises(FetchError): validate_public_url(url,resolve=False)


def test_public_url_syntax():
    validate_public_url('https://www.city.fukuoka.lg.jp/',resolve=False)


def test_url_canonicalization():
    assert canonical_url('https://EXAMPLE.com/a?x=1&utm_source=x#section')=='https://example.com/a?x=1'


def test_external_recruitment_requires_official_link():
    assert not allowed_link('https://public-connect.jp/job/1',{'municipal.example'})
    assert allowed_link('https://public-connect.jp/job/1',{'municipal.example'},True)
    assert not allowed_link('https://evil.example/job',{'municipal.example'},True)


def test_registry_prefectures_all_47():
    html='<html>'+''.join(f'<a href="/spd/map-search/region/{i}.html">{p}</a>' for i,p in enumerate(PREF_NAMES))+'</html>'
    assert len(registry_pages(html.encode()))==47


def test_registry_includes_wards_but_excludes_other_links():
    html='''<a href="https://www.pref.osaka.lg.jp/">大阪府</a>
    <a href="https://www.city.osaka.lg.jp/">大阪市</a>
    <a href="https://www.city.osaka.lg.jp/kita/">大阪市北区</a>
    <a href="https://www.example.com/">大阪府の観光</a>
    <a href="https://www.j-lis.go.jp/map.html">大阪市</a>'''
    rows=municipalities(html.encode(),'大阪府','https://www.j-lis.go.jp/map.html')
    assert len(rows)==3
    assert next(x for x in rows if x['name']=='大阪市北区')['kind']=='ward'


FIXED=datetime.fromisoformat('2026-11-13T20:00:00+09:00')
@pytest.mark.parametrize('payload,expected',[
    ({'deadline_at':'2026-11-12T23:59:59+09:00'},'closed'),
    ({'opens_at':'2026-12-01T00:00:00+09:00','deadline_at':'2026-12-27T23:59:59+09:00'},'upcoming'),
    ({'opens_at':'2026-11-01T00:00:00+09:00','deadline_at':'2026-11-24T17:00:00+09:00'},'open'),
    ({'deadline_at':'2026-11-24T17:00:00+09:00'},'unknown'),
    ({'explicit_closed':True},'closed'),
    ({'rolling':True,'acceptance_confirmed':True},'rolling'),
    ({'source_changed':True,'opens_at':'2026-12-01T00:00:00+09:00','deadline_at':'2026-12-27T23:59:59+09:00'},'unknown'),
])
def test_status(payload,expected):
    assert store.deadline_status(payload,FIXED)==expected


def test_deduplication_and_changes(source):
    doc=read_document(HTML.encode(),source['url']); payload,publish=extract_job(doc,source,source['url']+'job')
    assert store.save_candidate(source['id'],source['url']+'job',payload,'hash1',True)==(True,False)
    assert store.save_candidate(source['id'],source['url']+'job',payload,'hash1',True)==(False,False)
    assert len(store.jobs())==1
    store.save_candidate(source['id'],source['url']+'job',payload,'hash2',True)
    assert len(store.history())==2


def test_fetch_error_not_automatic_closed(source):
    payload={'title':'test','deadline_at':'2099-01-01T17:00:00+09:00','acceptance_confirmed':True}
    url=source['url']+'job';store.save_candidate(source['id'],url,payload,'abc',True)
    store.mark_job_error(url,'HTTP 503')
    j=store.jobs()[0]
    assert j['status']=='unknown'
    assert not j.get('explicit_closed')


def test_verified_change_requires_review(source):
    url=source['url']+'job';payload={'title':'test','salary_text':'original','acceptance_confirmed':True,'deadline_at':'2099-01-01T17:00:00+09:00'}
    store.save_candidate(source['id'],url,payload,'hash1',True)
    with store.connect() as c:c.execute("UPDATE jobs SET verification='manual'")
    changed={**payload,'salary_text':'new'}
    store.save_candidate(source['id'],url,changed,'hash2',True)
    row=store.jobs()[0]
    assert row['salary_text']=='original'
    assert row['status']=='unknown'
    assert store.history()[0]['kind']=='review_required'


class FakeFetcher:
    def __init__(self,mapping): self.mapping=mapping;self.calls=[]
    def get(self,url):
        self.calls.append(url)
        value=self.mapping.get(url)
        if isinstance(value,Exception):raise value
        if value is None:raise FetchError('not configured in offline fixture')
        return Response(url,value.encode(),'text/html')


def test_crawler_end_to_end_with_fixture(source):
    f=FakeFetcher({source['url']:'<html><h1>試験市</h1><a href="/jobs">職員採用</a></html>',
       source['url']+'jobs':'<html><h1>職員採用情報</h1><a href="/job/1">広報戦略専門職の採用募集</a></html>',
       source['url']+'job/1':HTML})
    result=crawl_source(source,f,10)
    assert result['pages']==3
    assert result['found']==1
    assert len(store.jobs())==1
    assert store.metadata()['visited_sources']==1


def test_run_metrics_are_real(source):
    store.set_setting('registry',{'state':'ok','checked_at':now_iso()})
    store.set_setting('national_registry',{'state':'ok','checked_at':now_iso()})
    result=run_crawl(limit=1,page_limit=4,fetcher=FakeFetcher({source['url']:HTML}))
    assert result['done']==1 and result['found']==1
    assert store.metadata()['last_run']['state']=='completed'


def test_registry_failure_preserves_sources(source):
    result=refresh_registry(FakeFetcher({}))
    assert result['state']=='error'
    assert len(store.sources())==1


def test_api_auth_filters_and_csv(monkeypatch):
    from app import main
    monkeypatch.setattr(main,'ADMIN_TOKEN','test-only-token')
    with TestClient(main.app) as c:
        assert c.get('/healthz').status_code==200
        assert c.get('/api/jobs').json()['total']==19
        assert c.get('/api/jobs?prefecture=大阪府').json()['total']==2
        assert c.get('/api/jobs?min_salary=8000000').json()['total']==1
        assert c.get('/api/admin/reviews').status_code==401
        assert c.get('/api/admin/reviews',headers={'Authorization':'Bearer test-only-token'}).status_code==200
        assert c.post('/api/admin/crawl',json={}).status_code==401
        assert c.get('/api/export.csv').status_code==200
        assert 'nosniff'==c.get('/').headers['x-content-type-options']


def test_csv_formula_protection():
    from app.main import csv_cell
    assert csv_cell('=1+1').startswith("'")
    assert csv_cell('@SUM(A1)').startswith("'")
    assert csv_cell('自治体')=='自治体'


def test_initial_curated_fingerprint_still_detects_early_closure(source):
    url=source['url']+'early'
    payload={'title':'test','salary_text':'curated','acceptance_confirmed':True,'deadline_at':'2099-01-01T17:00:00+09:00'}
    store.save_candidate(source['id'],url,payload,'first',True)
    with store.connect() as c:
        c.execute("UPDATE jobs SET verification='reference',content_hash=NULL")
    store.save_candidate(source['id'],url,{**payload,'explicit_closed':True},'new',True)
    row=store.jobs()[0]
    assert row['status']=='closed'
    assert row['salary_text']=='curated'


def test_new_ambiguity_removes_automatic_job_from_public_list(source):
    url=source['url']+'ambiguous'
    payload={'title':'test','deadline_at':'2099-01-01T17:00:00+09:00','acceptance_confirmed':True}
    store.save_candidate(source['id'],url,payload,'first',True)
    store.save_candidate(source['id'],url,{**payload,'deadline_at':None},'changed',False)
    assert not store.jobs()
    assert store.jobs(True)[0]['visibility']=='review'
