"""Application settings; no paid search or AI API is required."""
from __future__ import annotations
import os
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv('DATA_DIR', str(ROOT / 'data')))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / 'jobs.sqlite3'
JST = ZoneInfo('Asia/Tokyo')
REGISTRY_URL = 'https://www.j-lis.go.jp/spd/map-search/cms_1069.html'
PREF_NAMES = '北海道 青森県 岩手県 宮城県 秋田県 山形県 福島県 茨城県 栃木県 群馬県 埼玉県 千葉県 東京都 神奈川県 新潟県 富山県 石川県 福井県 山梨県 長野県 岐阜県 静岡県 愛知県 三重県 滋賀県 京都府 大阪府 兵庫県 奈良県 和歌山県 鳥取県 島根県 岡山県 広島県 山口県 徳島県 香川県 愛媛県 高知県 福岡県 佐賀県 長崎県 熊本県 大分県 宮崎県 鹿児島県 沖縄県'.split()
REGION_GROUPS = {
    '北海道': ['北海道'], '東北': PREF_NAMES[1:7], '関東': PREF_NAMES[7:14],
    '甲信越・北陸': PREF_NAMES[14:20], '東海': PREF_NAMES[20:24],
    '近畿': PREF_NAMES[24:30], '中国': PREF_NAMES[30:35],
    '四国': PREF_NAMES[35:39], '九州・沖縄': PREF_NAMES[39:47],
}
PREFECTURES = [{'code': f'{i+1:02d}', 'name': name,
                'region': next(r for r, ps in REGION_GROUPS.items() if name in ps)}
               for i, name in enumerate(PREF_NAMES)]
REGION_OF = {p['name']: p['region'] for p in PREFECTURES}
VERSION = '2.1.0'
NATIONAL_LABEL = '国の機関'
NATIONAL_REGISTRY_URL = 'https://www.e-gov.go.jp/government-directory/ministries-and-agencies.html'

def now_iso() -> str:
    return datetime.now(JST).isoformat(timespec='seconds')

def env_bool(name: str, default: bool=False) -> bool:
    return os.getenv(name, str(default)).lower() in ('1','true','yes','on')

SCHEDULER_ENABLED = env_bool('SCHEDULER_ENABLED', False)
CRAWL_ON_START = env_bool('CRAWL_ON_START', False)
DAILY_HOUR_JST = max(0, min(23, int(os.getenv('DAILY_HOUR_JST','5'))))
WORKERS = max(1, min(16, int(os.getenv('CRAWL_WORKERS', '6'))))
PAGES_PER_SOURCE = max(4, min(100, int(os.getenv('PAGES_PER_SOURCE', '24'))))
REQUEST_DELAY = max(1.0, float(os.getenv('REQUEST_DELAY', '1.5')))
USER_AGENT = os.getenv('CRAWLER_USER_AGENT', 'CivicPRJobs/2.0 (+administrative-recruitment-research; respects robots.txt)')
ADMIN_TOKEN = os.getenv('ADMIN_TOKEN','')