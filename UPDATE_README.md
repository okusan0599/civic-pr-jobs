# 巡回強化アップデート v2.1

このファイルは既存の civic-pr-jobs 向けの差し替え版です。公開中のサイトには、下記の操作をするまで反映されません。

## 反映する手順

1. civic_pr_jobs_crawl_update.zip を展開します。
2. GitHub の civic-pr-jobs を開き、Code タブで main を選びます。「app」の中ではなく、app・data・scripts・web が並んでいる一番上の階層です。
3. Add file > Upload files を開き、展開したフォルダーの中身（app / scripts / web / tests の4フォルダーと UPDATE_README.md）をドラッグします。外側の civic_pr_jobs_crawl_update フォルダーやZIPそのものはアップロードしません。
4. Commit changes で main に保存します。例： Upgrade crawl monitoring to v2.1
5. Actions > Daily collection and Pages > Run workflow を開き、main を選んで Run workflow を押します。以前の実行の Re-run jobs ではなく、最新の main で新しく実行してください。
6. collect と deploy が終わったら、既存の公開URLを再読み込みし、「収集状況」を開きます。CRAWL EVIDENCE / v2.1 が表示されれば反映済みです。古い画面が残る場合は Mac で Command + Shift + R を押してください。

## 今回変えないもの

- 公開URL、Pages設定、日次実行の時刻、daily-pages.yml はそのままです。YAMLの貼り直しは不要です。
- 既存の募集データは crawl-state ブランチから復元されます。このブランチは削除しないでください。ZIPにデータベースや初期募集データの差し替えは含みません。
- 更新後の初回実行で、記録用の列・テーブルを追加します。募集データを消す処理ではありません。

## 追加した確認項目

登録機関数 / 巡回試行数 / 採用ページ確認数（7日以内）/ 未巡回数 / 採用ページ未到達 / 取得失敗数。都道府県別・機関別の日時、エラーの原因、未処理URL、巡回履歴も表示します。

登録済み機関の中での確認率であり、全国の全求人の網羅率ではありません。国の機関は e-Gov の行政機関一覧が起点で、地方支分部局・関連法人まで全機関網羅しているわけではありません。

採用ページ到達は、どこかの採用案内または関連する募集本文を取得したことを表します。すべての募集を読み終えた意味ではありません。

収集先CSVは画面の絞り込み条件を反映します。全件を出すときは「リセット」の後に出力してください。公開時にsources.json・sources.csv・crawl-log.jsonも出力します。

## 検証範囲

自動テスト 96 項目に合格。PC・スマートフォン画面、検索、CSV出力、ページ送り、更新停止の警告を確認しました。テストの架空機関は公開データに入りません。

制作環境の通信制限により、現在のGitHub公開データ・実行ログの取得、全国実サイトでの到達率の確認はできていません。反映後の実行結果を「収集状況」で確認してください。

## Official GitHub documentation
- https://docs.github.com/en/repositories/working-with-files/managing-files/adding-a-file-to-a-repository
- https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow
