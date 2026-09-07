# monthly-yoy-watch

日本の上場企業がTDnet(適時開示情報閲覧サービス)で公表する「月次売上高」等の開示PDFを毎日自動でチェックし、
前年同月比(または増減率)が **±30pt以上** 変化した項目があれば Discord に通知するボット。

## 仕組み

1. 毎日 [cron-job.org](https://cron-job.org) から GitHub Actions の `workflow_dispatch` API を叩いて起動
   (GitHub Actions 自体の `schedule` トリガーは実行遅延・スキップが多く不安定なため使用していない)
2. `https://www.release.tdnet.info/inbs/I_list_001_YYYYMMDD.html` からその日の適時開示一覧を取得。
   開示が多い日は1ページに収まらず `I_list_002_...`, `I_list_003_...` と分割されるため、
   404になるまで全ページ取得して連結する(1ページ目しか見ていないと、開示の多い日に
   半分近くを見落とすことがある。実例: 2026-09-07は全93件中46件が2ページ目にあった)。
3. タイトルに「月次」「速報」を含む開示に加え、「売上」「既存店」を含み具体的な月への
   言及(例: 「7月度」「8月の」)がある開示も抽出する(「月次」「速報」という単語を
   使わずに月次売上を報告する開示があるため。例:「○月度の売上概況」「連結売上収益報告」)
4. 各PDFをダウンロードし、以下のいずれかで解析
   - **Claude判定(`ANTHROPIC_API_KEY` 設定時、優先)**: PDFファイルそのものをClaudeに渡し、
     「直近対象月の前年同月比・増減率」だけを判定させる。参考掲載されている前年実績値そのもの
     (前年同月比の計算結果ではない、単なる過去の数値)を誤って変化率として拾わないよう、
     プロンプトで明示的に除外を指示している。複数事業グループ・複数ページにまたがる開示は
     全ページ・全セクションを確認するよう指示している。
     (以前はpdfplumberで抽出したテキストを渡していたが、罫線なし・変則レイアウトのPDFで
     数字が一文字ずつ分解されるなどの破損が発生し誤検知の原因になったため、PDFを直接渡す
     方式に変更した。)
   - **ヒューリスティック解析(フォールバック、または `ANTHROPIC_API_KEY` 未設定時)**:
     pdfplumberでテキスト・表を抽出し、以下の順で正規表現ベースに解析
     - 罫線付き表(月ヘッダー横並び)
     - 罫線なし・月ヘッダー行+値行のペア
     - 罫線なし「実績/前年/前年比」形式の行
     - 「前年同月比 XX.X%」に直接隣接する数値(最終手段の狭いフォールバック)
5. 前年同月比が100%から+30pt以上(または増減率そのものが+30%以上)のプラス項目を抽出
6. 検知した項目があれば Discord Webhook に投稿
7. 処理済み開示のIDを `state/notified.json` に記録してリポジトリにコミット(再実行時の重複通知防止)

## 前年同月比の解釈について

Claude判定が有効な場合は、上記の注意点をプロンプトに含めた上でClaude自身に文脈から判断させる。
`ANTHROPIC_API_KEY` 未設定時やClaude呼び出し失敗時は、以下のヒューリスティックにフォールバックする
(PDFの書式は企業によってバラバラなため、あくまで推定):

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

### Claude判定の設定(推奨)

ヒューリスティック解析は、企業が参考として載せている「前年実績そのものの数値」を誤って
前年同月比として拾ってしまうことがある。これを避けるため、Claudeにその判定を代行させられる。

1. **APIキーを発行**: https://console.anthropic.com/settings/keys でAPIキーを作成
   (Proプランのブラウザ/デスクトップ利用枠とは別に、従量課金のAPI利用として契約が必要)
2. **リポジトリの Secrets に `ANTHROPIC_API_KEY` を追加**
   (Settings → Secrets and variables → Actions → Secrets タブ → New repository secret)
3. **(必須推奨) Anthropic Console 側で使用上限を設定**: https://console.anthropic.com/settings/limits
   などのBilling/Usageページから、月あたりの利用上限額を自分で設定する
   (これはAnthropicアカウントの課金設定なので、必ずご自身のアカウントで設定してください)
4. **(任意) 実行1回あたりの呼び出し回数の上限**: リポジトリの Variables
   (Settings → Secrets and variables → Actions → Variables タブ) に以下を追加すると、
   コードを変更せずに自分でチューニングできる
   - `MAX_CLAUDE_CALLS_PER_RUN`: 1回の実行で判定するPDFの最大件数(未設定時は40)。
     これを超えた分は自動でヒューリスティック解析にフォールバックする
   - `ANTHROPIC_MODEL`: 使用するモデルID(未設定時は `claude-sonnet-5`)。
     コストを抑えたい場合は `claude-haiku-4-5-20251001` に変更可能

**コスト目安**: 月次開示PDFは1〜3ページ程度で、1件あたりのAPI呼び出しは数千トークン程度。
月初の発表集中期でも1日数十件程度なので、`claude-sonnet-5` でも月間で数百円〜数千円程度、
`claude-haiku-4-5-20251001` ならさらに安く収まる想定。ただし実際の請求はAnthropic Console側の
利用状況を確認すること。

`ANTHROPIC_API_KEY` を設定しなければ、これまで通りヒューリスティック解析のみで動作する
(追加コストなし)。

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

- 縦書き・画像ベース(スキャンPDF)など、テキストとして抽出できないレイアウトは
  Claude判定・ヒューリスティックのどちらでも解析できず見逃す場合があります。
- Claude判定は「参考掲載の前年実績」と「前年同月比そのもの」の混同を減らすよう設計しているが、
  レイアウトが極端に複雑な場合はまれに誤判定する可能性がある。`ANTHROPIC_API_KEY` 未設定時、
  および呼び出し失敗時・`MAX_CLAUDE_CALLS_PER_RUN` 超過時はヒューリスティックにフォールバックし、
  その場合は従来通り「比率(100基準)」か「増減率(0基準)」かの判定を推定に頼る。
- TDnetの一覧ページの列構成が変わった場合、パーサの修正が必要になります。
