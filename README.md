# CIVIC PR JOBS v2

国・自治体の広報／PR／プロモーション領域に特化した採用情報の検索・収集アプリ。

**全国全件収集・公開稼働済みではありません。** 同梱データは19件（要確認17・終了2）。47都道府県のキーワード調査と、個別自治体巡回の実績を区別しています。全国名簿の実取得は作成環境の外部通信制限で失敗しました。起動・公開は [START_HERE.md](START_HERE.md) を参照してください。

## 構成

- `app/`：FastAPI、SQLite、国／地方の名簿取得、HTML/PDF収集、抽出・判定、管理API。
- `web/index.html`：検索、機関・地域・職務・給与・任期のフィルター、最大3件比較、ブラウザー内保存、CSV、収集状況、管理。
- `Preview.html`：初期調査データを含む閲覧版。単体では新規取得しません。
- `data/seed_jobs.json`：募集・確認候補19件。受付状況、調査方法、原典根拠を保持。
- `data/seed_sources.json`：国の機関45件の初期入口。全国全機関を登録済みとはしません。
- `data/research_audit.json`：47都道府県キーワード調査の範囲と限界。
- `.github/workflows/daily-pages.yml`：日次収集→状態保存→静的検索画面公開。未配備。
- `render.yaml`：管理画面つき有料サーバー方式。未配備。
- `docs/`：参照元、検証結果、実際の名簿取得失敗ログ。

初回のジョブ起動では、国の機関と地方機関の名簿を独立して取得します。一方の失敗で他方の取得まで飛ばさず、既存名簿は消しません。全国の登録対象と、実際に採用ページへ到達できた収集先を分けてカウントします。

## 起動

```bash
python -m venv .venv
# Mac / Linux:
. .venv/bin/activate
pip install -r requirements.txt
python -m app.cli serve --open
```

Windowsは `.venv\Scripts\activate` を利用します。付属の起動ファイルでも起動できます。

```bash
# 名簿を更新
python -m app.cli registry
# 全登録先を巡回。名簿未取得の場合は先に更新
python -m app.cli crawl
# 国のみ／都道府県別
python -m app.cli crawl --prefecture '国の機関'
python -m app.cli crawl --prefecture '東京都'
# 上限を設け、未巡回・古い確認先を優先して次回へつなぐ
python -m app.cli crawl --seconds 14400 --pages 24
# 画面／データの出力
python scripts/export_static.py --out Preview.html
python scripts/export_data.py --out exports
# SQLiteの一貫したバックアップ
python -m app.cli backup ../backup.sqlite3
```

## 更新と安全性

robots.txt、ホストごとの間隔、応答サイズ、タイムアウト、リダイレクト先の安全性を確認します。標準は同時6収集先、1収集先24ページ、1ホスト1.5秒以上の間隔です。サイト側の制限があれば取得せず未確認として扱います。

一般求人・広告入札・広報受託・合格発表は分類から除外します。複数職種が混在している募集や日付が曖昧なものは要確認キューに入れます。任用開始・生年月日・ハローワーク紹介期限を応募締切と混同しないように、原典と文脈を確認する必要があります。

手動確認済みの条件に変更が出たときは旧条件を無断上書きせず、再確認を要求します。失敗・404だけで終了と断定せず、明示終了と過去の締切を別に扱います。再確認から7日以上経過した非終了案件は要確認です。

国の機関種別と勤務地を別管理しています。関連法人・観光団体は国や自治体の直接雇用と区別しています。初期表示は「直接採用／キャリア・専門職／終了以外」です。

## API

`/api/jobs` のパラメーター：`q`, `entity_type`, `region`, `prefecture`, `status`, `category`, `tag`, `min_salary`, `offset`, `limit`。

`entity_type=government` は国・都道府県・市区町村・行政区の直接採用をまとめて検索します。`national`, `prefecture`, `municipality`, `ward`, `public_body` は個別指定です。勤務地は `prefecture` に指定します。

`/api/meta`, `/api/sources`, `/api/research-audit`, `/api/history`, `/api/export.csv` は公開読取用。`/api/admin/*` はBearerトークンが必要です。詳細は起動後の `/api/docs` にあります。

## 環境変数

`DATA_DIR`, `ADMIN_TOKEN`, `SCHEDULER_ENABLED`, `CRAWL_ON_START`, `DAILY_HOUR_JST`, `CRAWL_WORKERS`, `PAGES_PER_SOURCE`, `REQUEST_DELAY`, `CRAWLER_USER_AGENT`。

管理トークンはコード・公開データ・リポジトリに含めないでください。外部公開時は1ワーカーと永続データ保存先を維持します。ブラウザー保存は同じブラウザー内に限られ、複数端末で同期しません。

## テスト

```bash
pip install -r requirements-dev.txt
pytest -q
```

結果と未検証事項は `docs/VALIDATION.md`。外部実サイトの網羅・抽出精度と、ホスティング側の実行成功は、オフラインの単体テストでは保証できません。
