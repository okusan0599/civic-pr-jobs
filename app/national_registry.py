"""Import executive agencies from the official e-Gov directory; no guessed URLs."""
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup
from .extract import decode_html, canonical_url
from .config import NATIONAL_REGISTRY_URL, NATIONAL_LABEL, now_iso
from .fetch import SafeFetcher
from . import store

def national_agencies(html: bytes, base_url: str=NATIONAL_REGISTRY_URL) -> list[dict]:
    soup=BeautifulSoup(decode_html(html),'html.parser')
    rows={}
    for a in soup.select('h2 a[href], h3 a[href]'):
        name=a.get_text(' ',strip=True)
        url=canonical_url(urljoin(base_url,a['href']))
        host=(urlsplit(url).hostname or '').lower()
        if not name or len(name)>70 or host.endswith('e-gov.go.jp') or not (host.endswith('.go.jp') or host.endswith('.lg.jp')):
            continue
        rows[name]=dict(name=name,pref=NATIONAL_LABEL,url=url,kind='national',provenance=base_url)
    return list(rows.values())

def refresh_national_registry(fetcher: SafeFetcher|None=None) -> dict:
    f=fetcher or SafeFetcher()
    try:
        response=f.get(NATIONAL_REGISTRY_URL)
        rows=national_agencies(response.body,response.url)
        if len(rows)<20: raise ValueError('国の機関のリンクが20件未満。構造変更として既存名簿を保持します。')
        for row in rows: store.add_source(**row)
        result=dict(state='ok',checked_at=now_iso(),imported=len(rows),source=NATIONAL_REGISTRY_URL,
            message=f'e-Gov掲載の国の機関{len(rows)}件を登録。地方支分部局・関連法人を網羅した数ではありません。')
    except Exception as e:
        result=dict(state='error',checked_at=now_iso(),source=NATIONAL_REGISTRY_URL,message=str(e)[:500])
    store.set_setting('national_registry',result)
    return result
