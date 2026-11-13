"""Conservative, deterministic extraction. Ambiguity stays visible; no invented pay/dates."""
from __future__ import annotations
import hashlib, io, json, re, unicodedata
from datetime import datetime
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode
from bs4 import BeautifulSoup
from .config import JST

FIELD = re.compile(r'広報|プロモーション|マーケティング|ブランド|魅力発信|情報発信|PR(?:担当|戦略|専門)|フィルムコミッション', re.I)
HIRING = re.compile(r'採用|職員.{0,15}募集|募集.{0,15}(?:職員|人材|アドバイザー)|任期付|会計年度任用|地域おこし協力隊|募集案内|求人')
PROCUREMENT = re.compile(r'入札|プロポーザル|受託(?:事業)?者|委託(?:事業)?者|業務委託.{0,12}(?:公募|募集)|指定管理者|広告(?:掲載|募集)|スポンサー募集')
HUB = re.compile(r'職員採用|職員募集|採用情報|人材募集|採用案内|任期付|経験者採用|社会人採用|人事課|人事委員会|会計年度|協力隊')
DEADLINE_LABEL = re.compile(r'受付期間|申込(?:み)?期間|募集期間|応募期間|応募締切|申込(?:み)?締切|申込(?:み)?期限|受付締切|応募期限|提出期限|募集締切|受験申込')
CLOSED = re.compile(r'(?:募集|受付|申込|応募)(?:は|を|が)?(?:既に)?(?:終了しました|終了しています|終了いたしました|終了致しました|締め切りました)|【募集終了】|\[募集終了\]|\(募集終了\)')
DATE = re.compile(r'(?:(令和|平成)\s*(元|\d{1,2})\s*年|(20\d{2})\s*[年/.-])?\s*(\d{1,2})\s*[月/.-]\s*(\d{1,2})\s*日?')
TIME = re.compile(r'(\d{1,2})\s*(?:時|:)\s*(\d{1,2})?\s*分?')
TAG_RULES = {
    '広報戦略':r'広報戦略|広報企画|戦略立案', 'シティプロモーション':r'シティプロモーション|魅力発信',
    'マーケティング':r'マーケティング', 'ブランディング':r'ブランド|ブランディング',
    'SNS・デジタル':r'SNS|ソーシャルメディア|デジタル|WEBサイト|ウェブサイト',
    '編集・制作':r'記事|取材|編集|ライティング|動画|デザイン', 'メディア・PR':r'メディア|報道|パブリシティ',
    '観光・移住':r'観光|移住|定住', '官民連携':r'官民連携|企業版ふるさと納税|財源|寄附',
    '映像・ロケ誘致':r'フィルムコミッション|ロケ誘致', '地域支援':r'自治協議会|自治会|地域コミュニティ',
}


def normalize(text: str) -> str:
    return unicodedata.normalize('NFKC',text).replace('\r','')


def canonical_url(url: str) -> str:
    p=urlsplit(url)
    query=[(k,v) for k,v in parse_qsl(p.query,keep_blank_values=True)
           if not k.lower().startswith('utm_') and k.lower() not in ('fbclid','gclid')]
    return urlunsplit((p.scheme.lower(),p.netloc.lower(),p.path or '/',urlencode(query),''))


def decode_html(body: bytes) -> str:
    # BeautifulSoup's UnicodeDammit handles Shift-JIS and EUC-JP municipal pages.
    from bs4 import UnicodeDammit
    d=UnicodeDammit(body, is_html=True)
    return d.unicode_markup or body.decode('utf-8',errors='replace')


def read_document(body: bytes, url: str, content_type: str='') -> dict:
    if 'pdf' in content_type.lower() or body.startswith(b'%PDF'):
        from pypdf import PdfReader
        reader=PdfReader(io.BytesIO(body))
        if len(reader.pages)>80:
            raise ValueError('PDF exceeds 80 pages; manual inspection required')
        text='\n'.join((p.extract_text() or '') for p in reader.pages)
        if len(text.strip())<80:
            raise ValueError('画像PDF・文字抽出不能。自動OCRをせず要確認にします。')
        text=normalize(text)[:400000]
        title=next((l.strip() for l in text.splitlines() if HIRING.search(l) and len(l)<180),'募集要項PDF')
        return {'title':title,'text':text,'links':[],'jobposting':None,'is_pdf':True}
    soup=BeautifulSoup(decode_html(body),'html.parser')
    jobposting=None
    for el in soup.select('script[type="application/ld+json"]'):
        try:
            obj=json.loads(el.get_text())
            items=obj if isinstance(obj,list) else obj.get('@graph',[obj])
            entries=[x for x in items if isinstance(x,dict) and x.get('@type')=='JobPosting']
            if len(entries)==1:
                jobposting=entries[0]
        except (ValueError,TypeError,AttributeError):
            pass
    h1s=[x.get_text(' ',strip=True) for x in soup.find_all('h1')]
    title=next((x for x in h1s if HIRING.search(x)),next((x for x in h1s if x),''))
    title=title or (soup.title.get_text(' ',strip=True) if soup.title else '')
    # Capture links before removing navigation. The crawler scores and bounds them separately.
    links=[]
    for a in soup.select('a[href], area[href], link[rel="alternate"]'):
        href=a.get('href','')
        if href.startswith(('mailto:','javascript:','tel:','#')):
            continue
        target=canonical_url(urljoin(url,href))
        if urlsplit(target).scheme not in ('http','https'):
            continue
        label=a.get_text(' ',strip=True) or a.get('alt','') or a.get('title','')
        links.append({'url':target,'label':normalize(label)[:250]})
    for bad in soup.select('script, style, nav, header, footer, aside, noscript, form'):
        bad.decompose()
    for sel in ['#tmp_contents','#main_content','#main-content','article','main','#contents','#content']:
        node=soup.select_one(sel)
        if node and len(node.get_text(strip=True))>100:
            break
    else:
        node=soup.body or soup
    text=normalize(node.get_text('\n',strip=True))
    return {'title':normalize(title)[:250],'text':text[:400000],'links':links,'jobposting':jobposting,'is_pdf':False}


def dates_in(text: str) -> list[dict]:
    found=[]; inherited_year=None
    for m in DATE.finditer(normalize(text)):
        era,ey,year,month,day=m.groups()
        if era:
            inherited_year=(2018 if era=='令和' else 1988)+(1 if ey=='元' else int(ey))
        elif year:
            inherited_year=int(year)
        if inherited_year is None:
            continue  # Do not guess a year from the current date.
        try:
            dt=datetime(inherited_year,int(month),int(day),tzinfo=JST)
        except ValueError:
            continue
        suffix=text[m.end():m.end()+35]
        tm=TIME.search(suffix)
        # A time must precede the next date or range delimiter, not belong to another endpoint.
        prefix=re.split(r'から|まで|～|~|\n|\d+月',suffix)[0]
        tm=TIME.search(prefix)
        precision='date'
        if tm:
            try:
                dt=dt.replace(hour=int(tm[1]),minute=int(tm[2] or 0))
                precision='minute'
            except ValueError:
                pass
        found.append({'dt':dt,'precision':precision,'span':m.span()})
    return found


def extract_deadline(text: str, jobposting: dict|None=None) -> dict:
    result={'opens_at':None,'deadline_at':None,'deadline_note':'締切未確認','deadline_evidence':''}
    lines=normalize(text).splitlines()
    choices=[]
    for i,line in enumerate(lines):
        if not DEADLINE_LABEL.search(line):
            continue
        chunk='\n'.join(lines[i:i+5])[:500]
        # Stop before unrelated employment or interview dates.
        chunk=re.split(r'任用期間|任期\s*$|採用予定日|面接日時|試験日',chunk,flags=re.M)[0]
        vals=dates_in(chunk)
        if not vals or len(vals)>2:
            continue
        is_range=bool(re.search(r'から|～|~|より',chunk)) and len(vals)==2
        is_end=bool(re.search(r'締切|期限|まで|必着|消印',chunk))
        if not (is_range or is_end):
            continue
        end=vals[-1]['dt']
        if vals[-1]['precision']=='date':
            end=end.replace(hour=23,minute=59,second=59)
        start=vals[0]['dt'].isoformat() if is_range else None
        choices.append({'opens_at':start,'deadline_at':end.isoformat(),
                        'deadline_note':('当日消印有効' if '消印' in chunk else '必着' if '必着' in chunk else
                                         '時刻指定あり' if vals[-1]['precision']=='minute' else '締切時刻は原典確認'),
                        'deadline_evidence':chunk[:300]})
    unique={x['deadline_at']:x for x in choices}
    if len(unique)==1:
        return next(iter(unique.values()))
    if len(unique)>1:
        result['deadline_note']='複数の締切候補。応募方法・職種ごとの確認が必要'
        result['deadline_evidence']='\n'.join(x['deadline_evidence'] for x in choices)[:700]
        return result
    if jobposting and jobposting.get('validThrough'):
        try:
            value=jobposting['validThrough']
            dt=datetime.fromisoformat(value.replace('Z','+00:00'))
            if dt.tzinfo is None:
                dt=dt.replace(tzinfo=JST)
            if len(value)==10:
                dt=dt.replace(hour=23,minute=59,second=59)
            result.update(deadline_at=dt.isoformat(),deadline_note='構造化データの有効期限・要原典確認',
                          deadline_evidence='JobPosting.validThrough: '+value)
        except (ValueError,TypeError):
            pass
    return result


def section_excerpt(text: str, labels: str, limit: int=500) -> str:
    lines=text.splitlines()
    for i,line in enumerate(lines):
        if re.search(labels,line):
            return '\n'.join(lines[i:i+9])[:limit]
    return ''


def classify(doc: dict) -> dict:
    title=doc['title']; text=doc['text']
    if PROCUREMENT.search(title):
        return {'candidate':False,'reason':'法人向け発注・入札案件'}
    if re.search(r'合格(?:者)?発表|試験結果|募集一覧|採用情報一覧|職員募集一覧',title):
        return {'candidate':False,'reason':'一覧・結果発表'}
    strong=bool(FIELD.search(title) and HIRING.search(title))
    responsibilities=section_excerpt(text,r'職務内容|職務の概要|業務内容|職務概要|仕事内容',800)
    medium=bool(HIRING.search(title) and FIELD.search(responsibilities))
    if not (strong or medium):
        return {'candidate':False,'reason':'広報専門の人材募集としての根拠不足'}
    multiple=bool(re.search(r'広報戦略監[・、].*専門監|募集職種一覧|複数職種',title))
    return {'candidate':True,'strong':strong,'multiple_roles':multiple,
            'reason':'職名と採用文脈が一致' if strong else '業務内容に広報の記載'}


def extract_job(doc: dict, source: dict, url: str) -> tuple[dict,bool]:
    title,text=doc['title'],doc['text']
    c=classify(doc)
    deadline=extract_deadline(text,doc.get('jobposting'))
    salary=section_excerpt(text,r'給与|報酬|給料|年収|月額',450)
    term=section_excerpt(text,r'任用期間|雇用期間|任期|契約期間',400)
    requirement=section_excerpt(text,r'応募資格|受験資格|応募条件|必要な経験|求める人材',650)
    responsibility=section_excerpt(text,r'職務内容|職務の概要|業務内容|職務概要|仕事内容',700)
    years=[int(x) for x in re.findall(r'(\d{1,2})\s*年以上',requirement)]
    tags=[k for k,rx in TAG_RULES.items() if re.search(rx,title+'\n'+responsibility,re.I)]
    employment=next((x for x in ['特定任期付職員','一般任期付職員','会計年度任用職員','地域おこし協力隊','任期付職員'] if x in title+' '+text),'要確認')
    category='support' if employment in ('会計年度任用職員','地域おこし協力隊') else 'career'
    annual=re.search(r'年収\s*(?:約|例|:)?\s*([\d,]+(?:\.\d+)?)\s*万円',salary)
    annual_min=int(float(annual[1].replace(',',''))*10000) if annual and not c.get('multiple_roles') else None
    # Automatic numerical salary never becomes an official guaranteed minimum.
    p={'title':title,'municipality':source['name'],'prefecture':source['pref'],'region':source['region'],
       'municipality_url':source['url'],'source_url':url,'role_level':'要確認',
       'category':category,'employment_type':employment,'work_style':'要確認','tags':tags,
       'summary':responsibility[:190] or '広報・プロモーションに関する人材募集候補。職務と応募資格は原典を確認してください。',
       'hiring_policy':section_excerpt(text,r'民間|即戦力|専門知識|経験を活か|経験を生か',300),
       'responsibilities':responsibility,'requirements':requirement,
       'experience_years':min(years) if years else None,'experience_note':'自動抽出の候補値。職務別・通算の区別は原典確認。',
       'experience_required':bool(re.search(r'経験.{0,25}(?:年以上|有する|有して)',requirement)),
       'salary_text':salary or '給与条件未確認','salary_annual_min':None,
       'salary_annual_candidate':annual_min,'salary_kind':'unconfirmed','salary_note':'自動抽出。年収検索には承認済み公表年収のみを使用。',
       'term_text':term or '任期未確認','term_years':None,'renewal_text':'要原典確認',
       'explicit_closed':bool(CLOSED.search(title+'\n'+text[:1000])),
       'acceptance_confirmed':bool(deadline['opens_at']),
       'evidence':[{'field':'応募期限','text':deadline['deadline_evidence'],'url':url},
                   {'field':'給与','text':salary[:250],'url':url}],
       'extraction_note':'複数職種の可能性。条件を混ぜず要確認にしています。' if c.get('multiple_roles') else c['reason'],
       **deadline}
    publish=bool(c.get('strong') and not c.get('multiple_roles') and deadline['deadline_at'])
    return p,publish


def content_fingerprint(doc: dict) -> str:
    return hashlib.sha256((doc['title']+'\n'+doc['text']).encode()).hexdigest()


def link_priority(label: str,url: str) -> int:
    s=0
    if FIELD.search(label) and HIRING.search(label): s+=120
    elif FIELD.search(label): s+=20
    if HUB.search(label): s+=55
    if HIRING.search(label): s+=30
    if re.search(r'saiyo|saiyou|recruit|shokuin|boshu|bosyu|jinji',url,re.I): s+=15
    if re.search(r'募集要項|募集案内|選考案内',label): s+=35
    if url.lower().endswith('.pdf'): s+=5
    if PROCUREMENT.search(label): s-=200
    if re.search(r'合格|結果|様式|履歴書|申込書',label): s-=100
    if re.search(r'sitemap|サイトマップ|新着情報|お知らせ一覧|組織から探す',label+' '+url): s+=8
    if re.search(r'\.rss|\.atom|rss|feed',url): s+=12
    return s