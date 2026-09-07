# monthly-yoy-watch

日本の上場企業がTDnet(適時開示情報閲覧サービス)で公表する「月次売上高」等の開示PDFを毎日自動でチェックし、
前年同月比(または増減率)が **±30pt以上** 変化した項目があれば Discord に通知するボット。

## 仕組み

1. 毎日 [cron-job.org](https://cron-job.org) から GitHub Actions の `workflow_dispatch` API を叩いて起動
   (GitHub Actions 自体の `schedule` トリガーは実行遅延・スキップが多く不安定なため使用していない)
2. `https://www.release.tdnet.info/inbs/I_list_001_YYYYMMDD.html` からその日の適時開示一覧を取得
3. タイトルに「月次」または「速報」を含む開示を抽出
4. 各PDFをダウンロードし、以下の順で表・数値を解析
   - 罫線付き表(月ヘッダー横並び)
   - 罫線なし・月ヘッダー行+値行のペア
   - 罫線なし「実績/前年/前年比」形式の行
   - 「前年同月比 XX.X%」に直接隣接する数値(最終手段の狭いフォールバック)
5. 前年同月比が100%から±30pt以上(または増減率そのものが±30%以上)の項目を抽出
6. 検知した項目があれば Discord Webhook に投稿
7. 処理済み開示のIDを `state/notified.json` に記録してリポジトリにコミット(再実行時の重複通知防止)

## 前年同月比の解釈について

PDFの書式は企業によってバラバラなため、以下のヒューリスティックで判定しています。

- 数値に `+`/`-`/`△`/`▲` の符号が明示されている → **増減率**とみなし、その値をそのまま使う
  (例: `前年同月比+5.8%` → +5.8pt)
- 符号がなく、項目名/文脈に「増減率」「伸び率」「成長率」を含む → **増減率**として扱う
- それ以外(符号なしの通常表記、月次開示で最も一般的な形式) → **比率(100基準)**として扱い、
  `値 - 100` を変化量とする
  (例: `既存店 106.9%` → +6.9pt)

## セットアップ

```bash
pip install -r requirements.txt
python scripts/check_monthly.py --date 2026-09-04 --dry-run  # ローカルで動作確認
```

GitHub リポジトリの Secrets に `DISCORD_WEBHOOK_URL` を設定すると、GitHub Actions から自動投稿されます。

### cron-job.org での毎日実行設定

GitHub Actions の `schedule` は不安定なため、外部の cron-job.org から `workflow_dispatch` を
API経由で毎日8:00 JSTに叩く運用にしている。設定手順:

1. **GitHubのfine-grained personal access tokenを発行**
   - https://github.com/settings/personal-access-tokens/new を開く
   - Repository access: "Only select repositories" → `monthly-yoy-watch` を選択
   - Permissions → Repository permissions → **Actions: Read and write** を設定
   - 有効期限は任意(90日〜1年など)で発行し、トークン文字列(`github_pat_...`)をコピー

2. **cron-job.org でジョブを作成**
   - https://cron-job.org にログイン(アカウントがなければ作成)
   - 「CREATE CRONJOB」→ 以下を設定
     - **Title**: `monthly-yoy-watch daily trigger`
     - **URL**: `https://api.github.com/repos/rimpeinishihara-cell/monthly-yoy-watch/actions/workflows/check.yml/dispatches`
     - **Request method**: `POST`
     - **Schedule**: 毎日 08:00, タイムゾーン `Asia/Tokyo`
     - **Headers** (Advanced → Headers) に以下を追加:
       - `Accept: application/vnd.github+json`
       - `Authorization: Bearer <上で発行したトークン>`
       - `X-GitHub-Api-Version: 2022-11-28`
       - `Content-Type: application/json`
     - **Request body** (Advanced → Body):
       ```json
       {"ref":"master"}
       ```
   - 保存すると、毎日8:00にこのジョブがGitHub APIを叩き、Actionsの `Monthly YoY Watch` ワークフローが起動する

3. 動作確認は cron-job.org の「Execute now」、または GitHub 側で
   `Actions` タブ → `Monthly YoY Watch` → `Run workflow` から手動実行できる。

## 既知の制限

- 縦書き・画像ベースなど極端に特殊なレイアウトのPDF(表がテキストとして規則的に抽出できないもの)は
  解析できず見逃す場合があります。
- 「比率(100基準)」か「増減率(0基準)」かの判定は上記ヒューリスティックによる推定であり、
  まれに誤判定する可能性があります。
- TDnetの一覧ページの列構成が変わった場合、パーサの修正が必要になります。
