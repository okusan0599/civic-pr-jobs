from datetime import datetime, timedelta
import json
import pytest
from fastapi.testclient import TestClient
from app import store
from app.config import ROOT, PREF_NAMES, now_iso, JST, NATIONAL_REGISTRY_URL
from app.national_registry import national_agencies, refresh_national_registry
from app.crawler import run_crawl
from app.fetch import FetchError, Response

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store,'DB_PATH',tmp_path/'v2.sqlite3')
    store.init_db(False)

class Fetcher:
    def __init__(self, html=None): self.html=html
    def get(self,url):
        if self.html is None: raise FetchError('offline fixture')
        return Response(url,self.html.encode(),'text/html')

def test_national_parser_keeps_same_host_different_agencies():
    html='<h2><a href="https://www.cao.go.jp/">内閣府</a></h2><h3><a href="https://www.cao.go.jp/agency/">架空試験委員会</a></h3><a href="https://example.com/">広告</a>'
    rows=national_agencies(html.encode())
    assert len(rows)==2 and all(x['kind']=='national' for x in rows)

def test_national_parser_rejects_foreign_and_egov_navigation():
    html='<h2><a href="https://www.e-gov.go.jp/">国の機関</a></h2><h3><a href="https://ad.example.com/">架空広告</a></h3>'
    assert national_agencies(html.encode())==[]

def test_national_structural_change_does_not_erase_sources():
    store.add_source('試験庁','国の機関','https://agency.example/',kind='national')
    assert refresh_national_registry(Fetcher('<html></html>'))['state']=='error'
    assert len(store.sources())==1

def test_national_official_fixture_registers_rows():
    html=''.join(f'<h2><a href="https://example.go.jp/{i}/">試験庁{i}</a></h2>' for i in range(21))
    assert refresh_national_registry(Fetcher(html))['state']=='ok'
    assert len(store.sources())==21

def test_initial_audit_is_not_crawl_success():
    store.init_db()
    m=store.metadata()
    assert len(m['research_audit']['prefectures'])==47
    assert m['visited_sources']==0 and m['healthy_sources']==0
    assert not m['research_audit']['full_crawl_completed']
    assert not m['research_audit']['scheduler_live']

def test_initial_seed_has_19_unique_records():
    store.init_db()
    rows=store.jobs()
    assert len(rows)==19 and len({j['id'] for j in rows})==19
    assert len({(j['source_id'],j['source_url']) for j in rows})==19

def test_initial_seed_idempotent():
    store.init_db(); total=len(store.sources())
    store.init_db()
    assert len(store.jobs())==19 and len(store.sources())==total

def test_national_workplace_is_not_registration_prefecture():
    store.init_db()
    jobs=[j for j in store.jobs() if j.get('entity_type')=='national']
    assert len(jobs)==2
    assert {j['prefecture'] for j in jobs}=={'東京都','大阪府'}
    sources={s['id']:s for s in store.sources()}
    assert all(sources[j['source_id']]['pref']=='国の機関' for j in jobs)

def test_expired_snapshot_is_closed_not_unknown():
    j={'snapshot_only':True,'deadline_at':'2026-10-31T17:15:00+09:00'}
    assert store.deadline_status(j,datetime.fromisoformat('2026-11-13T20:00:00+09:00'))=='closed'

def test_search_index_not_confirmation_of_open():
    j={'snapshot_only':True,'acceptance_confirmed':True,'deadline_at':'2099-01-01T12:00:00+09:00'}
    assert store.deadline_status(j)=='unknown'

def test_old_verified_record_becomes_unknown():
    sid=store.add_source('試験市','東京都','https://city.example/')
    payload={'title':'test','deadline_at':'2099-01-01T17:00:00+09:00','acceptance_confirmed':True,'verified_at':(datetime.now(JST)-timedelta(days=8)).isoformat()}
    store.save_candidate(sid,'https://city.example/job',payload,'hash',True)
    assert store.jobs()[0]['stale'] and store.jobs()[0]['status']=='unknown'

def test_budget_exhaustion_is_not_full_completion():
    store.add_source('試験市','東京都','https://city.example/')
    for key in ('registry','national_registry'): store.set_setting(key,{'state':'ok','checked_at':now_iso()})
    r=run_crawl(max_seconds=0,fetcher=Fetcher())
    assert r['done']==0 and r['state']=='partial'
    assert store.metadata()['last_run']['total']==1

def test_unknown_salary_stays_out_of_high_salary_filter():
    from app.main import filter_jobs
    assert filter_jobs([{'salary_annual_min':None}],min_salary=8000000)==[]

def test_api_national_and_public_body_filters(monkeypatch):
    from app import main
    monkeypatch.setattr(main,'ADMIN_TOKEN','v2-test-token')
    with TestClient(main.app) as client:
        assert client.get('/api/jobs?entity_type=national').json()['total']==2
        assert client.get('/api/jobs?entity_type=public_body').json()['total']==5
        assert client.get('/api/jobs?entity_type=government').json()['total']==14
        assert client.get('/api/research-audit').json()['full_crawl_completed'] is False

def test_ambiguous_next_round_clearly_labelled():
    store.init_db()
    j=next(j for j in store.jobs() if j['id']=='niigata-pr-job-2027-1')
    assert '要確認' in j['title'] and j['status']=='unknown'

def test_no_national_generic_link_mislabelled_as_job():
    store.init_db()
    for j in store.jobs():
        if j.get('entity_type')=='national':
            assert j['source_url_is_job'] is False
            assert j.get('snapshot_only') is True