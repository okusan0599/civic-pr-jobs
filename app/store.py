"""SQLite persistence, change history and explicit verification states."""
from __future__ import annotations
import contextlib, hashlib, json, sqlite3
from datetime import datetime, timedelta
from typing import Any
from .config import DB_PATH, ROOT, PREFECTURES, REGION_OF, JST, now_iso


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def uid(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:20]


@contextlib.contextmanager
def connect():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA busy_timeout=30000')
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init_db(seed: bool=True) -> None:
    with connect() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, pref TEXT NOT NULL, region TEXT NOT NULL,
          url TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'municipality',
          enabled INTEGER NOT NULL DEFAULT 1, provenance TEXT NOT NULL DEFAULT '',
          last_checked TEXT, last_success TEXT, state TEXT NOT NULL DEFAULT 'unvisited',
          error TEXT, pages_count INTEGER NOT NULL DEFAULT 0,
          entrypoints TEXT NOT NULL DEFAULT '[]', cursor INTEGER NOT NULL DEFAULT 0,
          UNIQUE(pref,name)
        );
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, source_id TEXT NOT NULL, url TEXT NOT NULL,
          payload TEXT NOT NULL, visibility TEXT NOT NULL DEFAULT 'review',
          verification TEXT NOT NULL DEFAULT 'automatic', content_hash TEXT,
          last_checked TEXT, last_success TEXT, source_state TEXT NOT NULL DEFAULT 'unverified',
          error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(source_id,url), FOREIGN KEY(source_id) REFERENCES sources(id)
        );
        CREATE TABLE IF NOT EXISTS pages (
          url TEXT PRIMARY KEY, source_id TEXT NOT NULL, title TEXT, content_hash TEXT,
          text_excerpt TEXT, last_checked TEXT, state TEXT, error TEXT,
          priority INTEGER NOT NULL DEFAULT 0,
          FOREIGN KEY(source_id) REFERENCES sources(id)
        );
        CREATE TABLE IF NOT EXISTS changes (
          id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, at TEXT NOT NULL,
          kind TEXT NOT NULL, summary TEXT NOT NULL, proposed_payload TEXT
        );
        CREATE TABLE IF NOT EXISTS runs (
          id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, state TEXT NOT NULL,
          total INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0,
          pages INTEGER NOT NULL DEFAULT 0, found INTEGER NOT NULL DEFAULT 0,
          errors INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS ix_pages_source ON pages(source_id, priority DESC);
        CREATE INDEX IF NOT EXISTS ix_jobs_source ON jobs(source_id);
        CREATE INDEX IF NOT EXISTS ix_changes_job ON changes(job_id, at DESC);
        ''')
        from .audit import ensure_schema
        ensure_schema(c)
    if seed:
        source_file = ROOT / 'data' / 'seed_sources.json'
        if source_file.exists():
            for source in json.loads(source_file.read_text(encoding='utf-8')):
                add_source(**source)
        seed_file = ROOT / 'data' / 'seed_jobs.json'
        if seed_file.exists():
            for j in json.loads(seed_file.read_text(encoding='utf-8')):
                sid = add_source(j['municipality'], '国の機関' if j.get('entity_type')=='national' else j['prefecture'], j['municipality_url'],
                                 kind=j.get('entity_type','municipality'),
                                 provenance='調査データ。名簿登録と実巡回は区別。', entrypoints=[j['source_url']])
                j['source_id'] = sid
                j['id'] = j.get('id') or uid(sid + j['source_url'])
                stamp = j.get('checked_at','2026-11-13T20:00:00+09:00')
                with connect() as c:
                    c.execute('''INSERT OR IGNORE INTO jobs
                      (id,source_id,url,payload,visibility,verification,last_checked,last_success,
                      source_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                      (j['id'],sid,j['source_url'],dumps(j),'published',j.get('verification','reference'),
                       stamp,stamp,j.get('source_state','reference'),stamp,stamp))
                    old=c.execute('SELECT payload,verification,content_hash FROM jobs WHERE id=?',(j['id'],)).fetchone()
                    if old and old['verification'] in ('reference','research') and not old['content_hash'] and not json.loads(old['payload']).get('entity_type'):
                        c.execute('UPDATE jobs SET payload=?,verification=?,source_state=? WHERE id=?',
                            (dumps(j),j.get('verification','research'),j.get('source_state','reference'),j['id']))


def add_source(name: str, pref: str, url: str, kind: str='municipality',
               provenance: str='', entrypoints: list[str]|None=None) -> str:
    sid=uid(pref+'|'+name)
    with connect() as c:
        old=c.execute('SELECT entrypoints FROM sources WHERE id=?',(sid,)).fetchone()
        entries=list(dict.fromkeys((json.loads(old[0]) if old else [])+(entrypoints or [])))
        c.execute('''INSERT INTO sources(id,name,pref,region,url,kind,provenance,entrypoints)
                     VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                     url=excluded.url,kind=excluded.kind,entrypoints=excluded.entrypoints''',
                  (sid,name,pref,REGION_OF.get(pref,'全国' if kind=='national' else ''),url,kind,provenance,dumps(entries)))
    return sid


def setting(key: str, default: Any=None) -> Any:
    with connect() as c:
        r=c.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
    return json.loads(r[0]) if r else default


def set_setting(key: str, value: Any) -> None:
    with connect() as c:
        c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,dumps(value)))


def sources() -> list[dict]:
    from .audit import enrich_sources
    with connect() as c:
        rows = [dict(r) for r in c.execute('SELECT * FROM sources ORDER BY pref,name')]
    return enrich_sources(rows)


def deadline_status(j: dict, now: datetime|None=None) -> str:
    now=now or datetime.now(JST)
    if j.get('explicit_closed'):
        return 'closed'
    if j.get('source_changed'):
        return 'unknown'
    end=j.get('deadline_at')
    start=j.get('opens_at')
    try:
        if end and now > datetime.fromisoformat(end):
            return 'closed'
        if j.get('snapshot_only') or j.get('source_state') in ('error','unverified'):
            return 'unknown'
        if start and now < datetime.fromisoformat(start):
            return 'upcoming'
        if end and (start or j.get('acceptance_confirmed')):
            return 'open'
        if j.get('rolling') and j.get('acceptance_confirmed'):
            return 'rolling'
    except (ValueError,TypeError):
        pass
    return 'unknown'


def jobs(include_review: bool=False) -> list[dict]:
    with connect() as c:
        q='SELECT * FROM jobs' if include_review else "SELECT * FROM jobs WHERE visibility='published'"
        rows=c.execute(q+' ORDER BY updated_at DESC').fetchall()
    out=[]
    for r in rows:
        j=json.loads(r['payload'])
        j.update(id=r['id'], source_id=r['source_id'], visibility=r['visibility'],
                 verification=r['verification'], checked_at=r['last_checked'],
                 last_success=r['last_success'], source_state=r['source_state'],
                 fetch_error=r['error'], updated_at=r['updated_at'])
        j['status']=deadline_status(j)
        if r['source_state'] in ('error','unverified') and j['status'] != 'closed':
            j['status']='unknown'
        try:
            j['stale']=(datetime.now(JST)-datetime.fromisoformat(j.get('verified_at') or r['last_success'])).days >= 7
        except (ValueError,TypeError):
            j['stale']=True
        if j['stale'] and j['status'] != 'closed':
            j['status']='unknown'
        out.append(j)
    return out


def save_candidate(sid: str, url: str, payload: dict, content_hash: str,
                   publish: bool=False) -> tuple[bool,bool]:
    """Changed verified jobs retain their last reviewed conditions, but become unconfirmed."""
    stamp=now_iso()
    jid=uid(sid+url)
    with connect() as c:
        old=c.execute('SELECT * FROM jobs WHERE source_id=? AND url=?',(sid,url)).fetchone()
        if not old:
            payload.update(id=jid,source_id=sid,source_url=url)
            c.execute('''INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                      (jid,sid,url,dumps(payload),'published' if publish else 'review','automatic',
                       content_hash,stamp,stamp,'ok',None,stamp,stamp))
            c.execute('INSERT INTO changes(job_id,at,kind,summary) VALUES(?,?,?,?)',
                      (jid,stamp,'new','公式サイトから新しい募集候補を取得'))
            return True,False
        jid=old['id']
        prev=json.loads(old['payload'])
        # A manually seeded HTML page is not enough to verify PDF salary and eligibility.
        # First crawl records its fingerprint without overwriting the curated source fields.
        changed=bool((old['content_hash'] and old['content_hash'] != content_hash) or (payload.get('explicit_closed') and not prev.get('explicit_closed')))
        if changed and old['verification'] != 'automatic':
            prev['source_changed']=True
            if payload.get('explicit_closed'):
                prev['explicit_closed']=True
            c.execute('''UPDATE jobs SET payload=?,content_hash=?,last_checked=?,last_success=?,
                         source_state='changed',error=NULL,updated_at=? WHERE id=?''',
                      (dumps(prev),content_hash,stamp,stamp,stamp,jid))
            c.execute('''INSERT INTO changes(job_id,at,kind,summary,proposed_payload)
                         VALUES(?,?,?,?,?)''',(jid,stamp,'review_required',
                         '原典に変更があります。旧条件を保持し、受付状況を要確認に変更しました。',dumps(payload)))
        elif old['verification']=='automatic':
            payload.update(id=jid,source_id=sid,source_url=url)
            c.execute('''UPDATE jobs SET payload=?,content_hash=?,last_checked=?,last_success=?,
                         source_state='ok',error=NULL,updated_at=?,
                         visibility=CASE WHEN visibility='hidden' THEN 'hidden' ELSE ? END WHERE id=?''',
                      (dumps(payload),content_hash,stamp,stamp,stamp if changed else old['updated_at'],
                       'published' if publish else 'review',jid))
            if changed:
                c.execute('INSERT INTO changes(job_id,at,kind,summary) VALUES(?,?,?,?)',
                          (jid,stamp,'updated','募集ページの本文に変更'))
        else:
            c.execute('''UPDATE jobs SET content_hash=?,last_checked=?,last_success=?,
                         source_state=?,error=NULL WHERE id=?''',
                      (content_hash,stamp,stamp,'changed' if prev.get('source_changed') else 'ok',jid))
    return False,changed


def mark_job_error(url: str, error: str) -> None:
    with connect() as c:
        c.execute("UPDATE jobs SET last_checked=?,source_state='error',error=? WHERE url=?",
                  (now_iso(),error[:350],url))


def metadata() -> dict:
    with connect() as c:
        total=c.execute('SELECT COUNT(*) FROM sources').fetchone()[0]
        visited=c.execute('SELECT COUNT(*) FROM sources WHERE last_checked IS NOT NULL').fetchone()[0]
        healthy=c.execute("SELECT COUNT(*) FROM sources WHERE state='ok'").fetchone()[0]
        problems=c.execute("SELECT COUNT(*) FROM sources WHERE state IN ('error','partial','blocked')").fetchone()[0]
        review=c.execute("SELECT COUNT(*) FROM jobs WHERE visibility='review'").fetchone()[0]
        last=c.execute('SELECT * FROM runs ORDER BY started_at DESC LIMIT 1').fetchone()
        per_pref=[dict(r) for r in c.execute('''SELECT pref,COUNT(*) AS registered,
                    SUM(CASE WHEN last_checked IS NOT NULL THEN 1 ELSE 0 END) AS visited,
                    SUM(CASE WHEN state='ok' THEN 1 ELSE 0 END) AS ok FROM sources GROUP BY pref''')]
    audit_file=ROOT/'data'/'research_audit.json'
    audit=json.loads(audit_file.read_text(encoding='utf-8')) if audit_file.exists() else {}
    from .audit import metrics as coverage_metrics
    return {'coverage_v3':coverage_metrics(),'research_audit':audit,'national_registry':setting('national_registry',{'state':'not_started','message':'e-Govの国の機関一覧は未更新です。初期登録の機関は未巡回です。'}),
            'generated_at':now_iso(),'prefectures':PREFECTURES,'registered_sources':total,
            'visited_sources':visited,'healthy_sources':healthy,'problem_sources':problems,
            'review_count':review,'last_run':dict(last) if last else None,
            'registry':setting('registry',{'state':'not_started','message':'全国名簿は初回更新時に取得します。'}),
            'per_prefecture':per_pref,'mode':'server','snapshot_date':'2026-11-13'}


def history(job_id: str|None=None) -> list[dict]:
    with connect() as c:
        if job_id:
            rows=c.execute('SELECT * FROM changes WHERE job_id=? ORDER BY id DESC LIMIT 50',(job_id,))
        else:
            rows=c.execute('SELECT * FROM changes ORDER BY id DESC LIMIT 100')
        return [dict(r) for r in rows]