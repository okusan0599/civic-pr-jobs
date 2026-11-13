"""Discover municipality URLs from the public J-LIS map, including designated-city wards."""
from __future__ import annotations
import re
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup
from .config import REGISTRY_URL, PREF_NAMES, now_iso
from .extract import decode_html, canonical_url
from .fetch import SafeFetcher, FetchError
from . import store


def registry_pages(html: bytes, base_url: str=REGISTRY_URL) -> list[tuple[str,str]]:
    soup=BeautifulSoup(decode_html(html),'html.parser')
    pairs=[]
    for a in soup.select('a[href],area[href]'):
        text=a.get_text(' ',strip=True) or a.get('alt','')
        text=re.sub(r'\s+','',text)
        pref=text if text in PREF_NAMES else '北海道' if re.match(r'^道[央南北東]',text) else None
        if not pref: continue
        target=canonical_url(urljoin(base_url,a['href']))
        if urlsplit(target).hostname=='www.j-lis.go.jp' and '/map-search/' in target:
            pairs.append((pref,target))
    return list(dict.fromkeys(pairs))


def municipalities(html: bytes, pref: str, base_url: str) -> list[dict]:
    soup=BeautifulSoup(decode_html(html),'html.parser'); rows={}
    for a in soup.select('a[href],area[href]'):
        name=a.get_text(' ',strip=True) or a.get('alt','')
        name=re.sub(r'\s+','',name).strip()
        if not (name==pref or re.fullmatch(r'[一-龥ぁ-んァ-ヶー々ヶヵ]{1,18}[市区町村]',name)):
            continue
        target=canonical_url(urljoin(base_url,a['href']))
        p=urlsplit(target)
        if p.scheme not in ('http','https') or not p.hostname or p.hostname.endswith('j-lis.go.jp'):
            continue
        kind='prefecture' if name==pref else 'ward' if re.search(r'市.+区$',name) else 'municipality'
        # Distinct ward entrypoints matter: a Fukuoka ward is not a separate municipality.
        rows[name]={'name':name,'pref':pref,'url':target,'kind':kind,'provenance':base_url}
    return list(rows.values())


def refresh_local_registry(fetcher: SafeFetcher|None=None) -> dict:
    f=fetcher or SafeFetcher()
    stamp=now_iso(); previous=store.setting('registry',{})
    store.set_setting('registry',{'state':'running','started_at':stamp,'message':'J-LISの全国公式リンク集を確認中'})
    try:
        r=f.get(REGISTRY_URL)
        pages=registry_pages(r.body,r.url)
        discovered={pref for pref,_ in pages}
        if len(discovered)!=47:
            raise ValueError(f'都道府県リンクは{len(discovered)}/47件。構造変更の可能性があるため既存名簿を保持します。')
        errors=[]; counts={}; imported=0; reports=[]
        for pref,url in pages:
            try:
                response=f.get(url)
                rows=municipalities(response.body,pref,response.url)
                if not rows and '北海道'!=pref:
                    raise ValueError('自治体リンクを抽出できませんでした')
                for row in rows:
                    store.add_source(**row)
                counts[pref]=counts.get(pref,0)+len(rows)
                imported+=len(rows)
                reports.append({'prefecture':pref,'url':url,'state':'ok','imported':len(rows),'checked_at':now_iso()})
            except (FetchError,ValueError) as e:
                errors.append({'prefecture':pref,'url':url,'error':str(e)[:250]})
                reports.append({'prefecture':pref,'url':url,'state':'error','imported':0,'checked_at':now_iso()})
        total=len([s for s in store.sources() if s['kind'] in ('prefecture','municipality','ward')])
        complete=not errors and len([p for p,n in counts.items() if n>0])==47 and total>=1700
        result={'state':'ok' if complete else 'partial','checked_at':now_iso(),
                'source':REGISTRY_URL,'prefectures_loaded':len([p for p,n in counts.items() if n>0]),
                'registered_sources':total,'errors':errors,'per_prefecture_imports':reports,'rows_per_prefecture':counts,
                'missing_prefectures':[p for p in PREF_NAMES if not counts.get(p)],
                'message':'47都道府県の自治体・行政区リンクを取得。巡回の網羅性を保証するものではありません。' if complete
                else '全国名簿の取得が一部未完了です。既存情報を保持し、次回再試行します。'}
        store.set_setting('registry',result)
        return result
    except Exception as e:
        result={'state':'error','checked_at':now_iso(),'source':REGISTRY_URL,
                'message':str(e)[:500],'previous_success':previous.get('checked_at')}
        store.set_setting('registry',result)
        return result

def refresh_registry(fetcher: SafeFetcher|None=None) -> dict:
    """National discovery runs independently even if J-LIS is unavailable."""
    from .national_registry import refresh_national_registry
    f=fetcher or SafeFetcher()
    national=refresh_national_registry(f)
    local=refresh_local_registry(f)
    return {**local, 'national_registry':national,
            'state':('ok' if local.get('state')=='ok' and national.get('state')=='ok' else 'error' if local.get('state')=='error' and national.get('state')=='error' else 'partial')}
