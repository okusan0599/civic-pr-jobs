"""Evidence-based crawl coverage. Registered institutions are not a national census."""
from __future__ import annotations
import json
from collections import Counter
from datetime import datetime, timedelta
from .config import JST, PREF_NAMES, now_iso

SOURCE_COLUMNS = {
    'audit_version': 'INTEGER NOT NULL DEFAULT 0',
    'last_run_id': 'TEXT', 'last_hub_success': 'TEXT', 'last_hub_url': 'TEXT',
    'attempted_pages': 'INTEGER NOT NULL DEFAULT 0',
    'fetched_pages': 'INTEGER NOT NULL DEFAULT 0',
    'parsed_pages': 'INTEGER NOT NULL DEFAULT 0',
    'failed_pages': 'INTEGER NOT NULL DEFAULT 0',
    'recruitment_pages': 'INTEGER NOT NULL DEFAULT 0',
    'candidate_pages': 'INTEGER NOT NULL DEFAULT 0',
    'new_candidates': 'INTEGER NOT NULL DEFAULT 0',
    'updated_candidates': 'INTEGER NOT NULL DEFAULT 0',
    'published_added': 'INTEGER NOT NULL DEFAULT 0',
    'review_added': 'INTEGER NOT NULL DEFAULT 0',
    'remaining_pages': 'INTEGER NOT NULL DEFAULT 0',
    'finish_reason': 'TEXT', 'elapsed_seconds': 'REAL NOT NULL DEFAULT 0',
    'error_details': "TEXT NOT NULL DEFAULT '[]'",
}
RUN_COLUMNS = {
    'attempted_pages': 'INTEGER NOT NULL DEFAULT 0',
    'parsed_pages': 'INTEGER NOT NULL DEFAULT 0',
    'failed_pages': 'INTEGER NOT NULL DEFAULT 0',
    'hub_sources': 'INTEGER NOT NULL DEFAULT 0',
    'site_only_sources': 'INTEGER NOT NULL DEFAULT 0',
    'failed_sources': 'INTEGER NOT NULL DEFAULT 0',
    'partial_sources': 'INTEGER NOT NULL DEFAULT 0',
    'budget_sources': 'INTEGER NOT NULL DEFAULT 0',
    'updated': 'INTEGER NOT NULL DEFAULT 0',
    'published_added': 'INTEGER NOT NULL DEFAULT 0',
    'review_added': 'INTEGER NOT NULL DEFAULT 0',
    'deferred_sources': 'INTEGER NOT NULL DEFAULT 0',
    'audit_version': 'INTEGER NOT NULL DEFAULT 0',
}

def ensure_schema(c):
    for table, columns in [('sources', SOURCE_COLUMNS), ('runs', RUN_COLUMNS)]:
        present = {r[1] for r in c.execute('PRAGMA table_info('+table+')')}
        for name, ddl in columns.items():
            if name not in present:
                c.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+ddl)
    # A URL can legitimately belong to multiple agencies on the same website.
    info = list(c.execute('PRAGMA table_info(pages)'))
    keys = {r[1] for r in info if r[5]}
    if keys == {'url'}:
        c.execute('DROP INDEX IF EXISTS ix_pages_source')
        c.execute('ALTER TABLE pages RENAME TO pages_v2_migration')
        c.execute('''CREATE TABLE pages (
          url TEXT NOT NULL, source_id TEXT NOT NULL, title TEXT, content_hash TEXT,
          text_excerpt TEXT, last_checked TEXT, state TEXT, error TEXT,
          priority INTEGER NOT NULL DEFAULT 0,
          discovered_at TEXT, depth INTEGER NOT NULL DEFAULT 0,
          page_kind TEXT NOT NULL DEFAULT 'unknown',
          PRIMARY KEY(source_id,url), FOREIGN KEY(source_id) REFERENCES sources(id))''')
        c.execute('''INSERT INTO pages(url,source_id,title,content_hash,text_excerpt,
          last_checked,state,error,priority,discovered_at)
          SELECT url,source_id,title,content_hash,substr(text_excerpt,1,2400),
          last_checked,state,error,priority,COALESCE(last_checked,?) FROM pages_v2_migration''', (now_iso(),))
        c.execute('DROP TABLE pages_v2_migration')
    present = {r[1] for r in c.execute('PRAGMA table_info(pages)')}
    for name, ddl in [('discovered_at','TEXT'), ('depth','INTEGER NOT NULL DEFAULT 0'),
                      ('page_kind',"TEXT NOT NULL DEFAULT 'unknown'")]:
        if name not in present:
            c.execute('ALTER TABLE pages ADD COLUMN '+name+' '+ddl)
    c.execute('CREATE INDEX IF NOT EXISTS ix_pages_source ON pages(source_id,priority DESC)')
    c.execute('''CREATE TABLE IF NOT EXISTS source_runs (
      run_id TEXT NOT NULL, source_id TEXT NOT NULL, started_at TEXT NOT NULL,
      finished_at TEXT NOT NULL, state TEXT NOT NULL, metrics TEXT NOT NULL,
      PRIMARY KEY(run_id,source_id))''')
    c.execute('CREATE INDEX IF NOT EXISTS ix_source_runs_recent ON source_runs(finished_at DESC)')


def error_code(exc) -> str:
    text = str(exc).lower()
    if 'robots' in text or 'crawl-delay' in text:
        return 'robots'
    if '429' in text:
        return 'rate_limited'
    if '403' in text or '401' in text:
        return 'access_denied'
    if '404' in text or '410' in text:
        return 'not_found'
    if '50' in text and 'http' in text:
        return 'server_error'
    if 'timeout' in text or 'timed out' in text:
        return 'timeout'
    if 'ssl' in text or 'certificate' in text:
        return 'tls'
    if 'name resolution' in text or 'getaddrinfo' in text or '\u540d\u524d\u89e3\u6c7a' in text:
        return 'dns_or_url_safety'
    if 'pdf' in text.lower():
        return 'pdf_extraction'
    if 'xml' in text.lower() or 'parse' in text:
        return 'parse'
    return 'other'


def enrich_sources(rows):
    from . import store
    with store.connect() as c:
        stats = {r['source_id']: dict(r) for r in c.execute('''SELECT source_id,
          COUNT(*) AS discovered_pages,
          SUM(CASE WHEN last_checked IS NULL THEN 1 ELSE 0 END) AS pending_pages,
          SUM(CASE WHEN state='error' THEN 1 ELSE 0 END) AS retry_pages
          FROM pages GROUP BY source_id''')}
    for s in rows:
        s.update({k: v for k, v in stats.get(s['id'], {}).items() if k != 'source_id'})
        for key in ('discovered_pages', 'pending_pages', 'retry_pages'):
            s.setdefault(key, 0)
        try:
            s['error_details'] = json.loads(s.get('error_details') or '[]')
        except (ValueError, TypeError):
            s['error_details'] = []
        s['stage'] = ('disabled' if not s.get('enabled') else
                      'unvisited' if not s.get('last_checked') else
                      'legacy' if not s.get('audit_version') else
                      'recruitment' if s.get('recruitment_pages') else
                      'site_only' if s.get('parsed_pages') else 'failed')
    return rows


def parse_time(value):
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=JST)
    except (ValueError, TypeError):
        return None


def metrics():
    from . import store
    now = datetime.now(JST)
    rows = store.sources()
    active = [s for s in rows if s['enabled']]
    cutoff = now - timedelta(days=7)
    def recent(s, key='last_hub_success'):
        dt = parse_time(s.get(key))
        return bool(dt and dt >= cutoff)
    def counts(items):
        return dict(
            registered=len(items),
            attempted=sum(bool(s['last_checked']) for s in items),
            unvisited=sum(not s['last_checked'] for s in items),
            reachable=sum(bool(s['last_success']) for s in items),
            recruitment_ever=sum(bool(s.get('last_hub_success')) for s in items),
            recruitment_recent=sum(recent(s) for s in items),
            recruitment_latest=sum(s['stage']=='recruitment' for s in items),
            site_only=sum(s['stage']=='site_only' for s in items),
            failed=sum(s['stage']=='failed' for s in items),
            legacy=sum(s['stage']=='legacy' for s in items),
            pending_pages=sum(s['pending_pages'] for s in items),
            retry_pages=sum(s['retry_pages'] for s in items),
            needs_attention=sum(s['stage'] in ('failed','site_only','legacy') or s['pending_pages']>0 or s['failed_pages']>0 for s in items),
        )
    with store.connect() as c:
        runs = [dict(r) for r in c.execute('SELECT * FROM runs ORDER BY started_at DESC LIMIT 30')]
        job_counts = dict(c.execute('SELECT visibility,COUNT(*) FROM jobs GROUP BY visibility').fetchall())
    per_pref = []
    for pref in PREF_NAMES + ['\u56fd\u306e\u6a5f\u95a2']:
        per_pref.append({'pref': pref, **counts([s for s in active if s['pref']==pref])})
    types = {kind: counts([s for s in active if s['kind']==kind])
             for kind in sorted({s['kind'] for s in active})}
    errors = Counter()
    for s in active:
        for code in {e.get('code','other') for e in s['error_details']}:
            errors[code] += 1
    local = store.setting('registry', {})
    national = store.setting('national_registry', {})
    return {'schema_version': 3, 'generated_at': now_iso(),
        'scope': 'Registered institutions only; not a complete census of all job openings.',
        'enabled_sources': len(active), 'disabled_sources': len(rows)-len(active),
        **counts(active), 'per_prefecture': per_pref, 'by_kind': types,
        'registry_complete': local.get('state')=='ok' and national.get('state')=='ok',
        'registered_prefectures': len({s['pref'] for s in active if s['pref'] in PREF_NAMES}),
        'registry_loaded_prefectures': local.get('prefectures_loaded',0),
        'published_jobs': job_counts.get('published',0), 'review_jobs': job_counts.get('review',0),
        'error_breakdown': dict(errors), 'runs': runs}


def source_run_history(limit=10000):
    from . import store
    with store.connect() as c:
        rows = c.execute('''SELECT h.*,s.name,s.pref,s.url FROM source_runs h
          JOIN sources s ON s.id=h.source_id ORDER BY h.finished_at DESC LIMIT ?''',(limit,)).fetchall()
    return [{**dict(r), 'metrics': json.loads(r['metrics'])} for r in rows]


def finish_source(source, result, run_id, started_at):
    from . import store
    stamp = now_iso()
    values = {
        'audit_version': 3, 'last_run_id': run_id,
        'attempted_pages': result['attempts'], 'fetched_pages': result['pages'],
        'parsed_pages': result['parsed'], 'failed_pages': len(result['errors']),
        'recruitment_pages': result['hubs'], 'candidate_pages': result['candidates'],
        'new_candidates': result['found'], 'updated_candidates': result['updated'],
        'published_added': result['published_added'], 'review_added': result['review_added'],
        'remaining_pages': result['remaining'], 'finish_reason': result['finish_reason'],
        'elapsed_seconds': round(result['elapsed'],2),
        'error_details': store.dumps(result['errors'][:12]),
    }
    with store.connect() as c:
        c.execute('UPDATE sources SET '+','.join(k+'=?' for k in values)+' WHERE id=?',
                  (*values.values(),source['id']))
        c.execute('''UPDATE sources SET last_checked=?,last_success=COALESCE(?,last_success),
          last_hub_success=COALESCE(?,last_hub_success),last_hub_url=COALESCE(?,last_hub_url),
          state=?,error=?,pages_count=? WHERE id=?''',
          (stamp,stamp if result['parsed'] else None,stamp if result['hubs'] else None,
           result.get('hub_url'),result['state'],
           store.dumps(result['errors'][:6]) if result['errors'] else
           ('Recruitment page not reached; not a zero-job result.' if not result['hubs'] else None),
           result['parsed'],source['id']))
        if run_id:
            payload = {k:v for k,v in result.items() if k!='errors'}
            payload['errors'] = result['errors'][:12]
            c.execute('INSERT OR REPLACE INTO source_runs VALUES(?,?,?,?,?,?)',
                (run_id,source['id'],started_at,stamp,result['state'],store.dumps(payload)))


def write_ci_summary():
    """Write actual counts to Actions; never treat a green deployment as crawl coverage."""
    import os
    from pathlib import Path
    from . import store
    path = os.getenv('GITHUB_STEP_SUMMARY')
    if not path:
        return
    m = store.metadata()
    a = m['coverage_v3']
    r = m.get('last_run') or {}
    text = '\n## Crawl evidence / '+now_iso()+'\n\n'
    text += '| Metric | Count |\n|---|---:|\n'
    for label,key in [('Registered institutions','registered'),('Attempted institutions (ever)','attempted'),
                      ('Recruitment pages reached (last 7 days)','recruitment_recent'),
                      ('Never attempted','unvisited'),('Latest attempt failed','failed'),
                      ('Unprocessed discovered URLs','pending_pages'),('Candidates awaiting review','review_jobs')]:
        text += '| '+label+' | '+str(a.get(key,0))+' |\n'
    text += '\nLast batch: **'+str(r.get('state','not_started'))+'**, '+str(r.get('done',0))+'/'+str(r.get('total',0))+' institutions.\n'
    text += '\nA successful Pages deployment is NOT evidence of nationwide recruitment coverage.\n'
    text += '\n### Registry status\n\n- Local: '+str(m['registry'].get('state','not_started'))+'\n- National: '+str(m['national_registry'].get('state','not_started'))+'\n'
    with Path(path).open('a',encoding='utf-8') as f:
        f.write(text)
    if not a['registry_complete']:
        print('::warning title=Registry incomplete::The registry is incomplete. Check the Collection Status page; missing institutions are not zero-job results.')
    if r.get('state') != 'completed':
        print('::warning title=Partial crawl::Some institutions or discovered URLs remain unconfirmed. A successful deployment does not imply complete coverage.')
