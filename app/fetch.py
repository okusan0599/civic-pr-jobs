"""Bounded HTTP fetching, robots.txt, host pacing and private-address protection."""
from __future__ import annotations
import ipaddress, socket, threading, time
from dataclasses import dataclass
from urllib.parse import urlsplit, urljoin
from urllib.robotparser import RobotFileParser
import requests
from .config import REQUEST_DELAY, USER_AGENT

MAX_BYTES=12*1024*1024

class FetchError(RuntimeError): pass
class RobotsBlocked(FetchError): pass

@dataclass
class Response:
    url: str
    body: bytes
    content_type: str
    status: int=200


def validate_public_url(url: str, resolve: bool=True) -> None:
    try:
        p=urlsplit(url)
        if p.scheme not in ('http','https') or not p.hostname or p.username or p.password:
            raise ValueError('Unsupported URL')
        if p.port not in (None,80,443):
            raise ValueError('Nonstandard port')
        host=p.hostname.rstrip('.').lower()
        if host in ('localhost','metadata.google.internal') or host.endswith(('.localhost','.local','.internal')):
            raise ValueError('Private host')
        try:
            addr=ipaddress.ip_address(host)
        except ValueError:
            addr=None
        if addr is not None and not addr.is_global:
            raise ValueError('Private address')
        if resolve:
            ips={x[4][0] for x in socket.getaddrinfo(host,p.port or (443 if p.scheme=='https' else 80),type=socket.SOCK_STREAM)}
            if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
                raise ValueError('Private or unresolved address')
    except (ValueError,OSError) as e:
        raise FetchError(f'URL安全性・名前解決の確認失敗: {e}') from e


class SafeFetcher:
    def __init__(self):
        self._guard=threading.Lock()
        self._locks: dict[str,threading.Lock]={}
        self._last: dict[str,float]={}
        self._robots: dict[str,tuple[RobotFileParser|None,float,str]]={}
        self._local=threading.local()

    def _session(self) -> requests.Session:
        if not hasattr(self._local,'session'):
            s=requests.Session()
            s.headers.update({'User-Agent':USER_AGENT,'Accept':'text/html,application/pdf,application/xml;q=0.9,*/*;q=0.2'})
            # Do not import ambient proxy credentials or netrc entries.
            s.trust_env=False
            self._local.session=s
        return self._local.session

    def _lock(self,host: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(host,threading.Lock())

    def _get_once(self,url: str,delay: float=REQUEST_DELAY) -> requests.Response:
        validate_public_url(url)
        host=urlsplit(url).netloc
        with self._lock(host):
            wait=delay-(time.monotonic()-self._last.get(host,0))
            if wait>0: time.sleep(wait)
            self._last[host]=time.monotonic()
            try:
                return self._session().get(url,timeout=(8,25),stream=True,allow_redirects=False)
            except requests.RequestException as e:
                raise FetchError(str(e)[:250]) from e

    def _read(self,r: requests.Response) -> bytes:
        try:
            try:
                if int(r.headers.get('Content-Length','0'))>MAX_BYTES:
                    raise FetchError('応答サイズが上限12MBを超えています')
            except ValueError:
                pass
            chunks=[]; size=0
            for chunk in r.iter_content(65536):
                size+=len(chunk)
                if size>MAX_BYTES:
                    raise FetchError('展開後応答サイズが上限12MBを超えています')
                chunks.append(chunk)
            return b''.join(chunks)
        finally:
            r.close()

    def _policy(self,url: str) -> tuple[RobotFileParser|None,float,str]:
        p=urlsplit(url); origin=f'{p.scheme}://{p.netloc}'
        with self._guard:
            cached=self._robots.get(origin)
        if cached:
            return cached
        robots_url=origin+'/robots.txt'
        try:
            r=self._get_once(robots_url)
            # Redirected robots must remain on the same host (HTTP to HTTPS is fine).
            for _ in range(3):
                if r.status_code not in (301,302,303,307,308): break
                target=urljoin(robots_url,r.headers.get('Location',''))
                r.close()
                if urlsplit(target).hostname!=p.hostname:
                    raise FetchError('robots.txtが別ドメインに転送されました')
                robots_url=target; r=self._get_once(target)
            if r.status_code in (404,410):
                r.close(); policy=(None,REQUEST_DELAY,'allow')
            elif r.status_code>=400 or 300<=r.status_code<400:
                code=r.status_code; r.close()
                policy=(None,REQUEST_DELAY,f'robots.txt取得不可 HTTP {code}')
            else:
                txt=self._read(r).decode('utf-8',errors='replace')
                rp=RobotFileParser(); rp.set_url(robots_url); rp.parse(txt.splitlines())
                delay=rp.crawl_delay(USER_AGENT) or rp.crawl_delay('*') or REQUEST_DELAY
                policy=(rp,max(float(delay),REQUEST_DELAY),'ok')
        except FetchError as e:
            policy=(None,REQUEST_DELAY,str(e))
        with self._guard:
            self._robots[origin]=policy
        return policy

    def get(self,url: str) -> Response:
        current=url
        for redirect in range(6):
            rp,delay,state=self._policy(current)
            if state not in ('ok','allow'):
                raise RobotsBlocked(state)
            if rp and not rp.can_fetch(USER_AGENT,current):
                raise RobotsBlocked('robots.txtにより巡回対象外')
            if delay>60:
                raise RobotsBlocked('長いCrawl-delay指定。自動巡回せず要確認')
            r=None
            for attempt in range(2):
                r=self._get_once(current,delay)
                if r.status_code not in (429,500,502,503,504) or attempt==1:
                    break
                retry=r.headers.get('Retry-After','3')
                r.close()
                try: pause=min(30,max(3,int(retry)))
                except ValueError: pause=3
                time.sleep(pause)
            assert r is not None
            if r.status_code in (301,302,303,307,308):
                current=urljoin(current,r.headers.get('Location',''))
                r.close(); validate_public_url(current)
                continue
            if r.status_code>=400:
                code=r.status_code; r.close()
                raise FetchError(f'HTTP {code}：原典を取得できませんでした')
            content_type=r.headers.get('Content-Type','')
            body=self._read(r)
            if not any(t in content_type.lower() for t in ('html','pdf','xml','text/plain')) and not body.startswith(b'%PDF'):
                raise FetchError('HTML・PDF・XML以外のファイルは対象外')
            return Response(current,body,content_type)
        raise FetchError('転送回数が上限を超えました')