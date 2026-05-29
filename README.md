# 洗剤・日用品 単価比較

洗剤・柔軟剤・食器用洗剤などを、**内容量あたりの客観的な単価（円/100g・円/100ml）** で比較するサイトです。価格は楽天24（楽天市場API）から取得し、毎日記録して価格推移・買い時も表示します。

## 比較のしくみ（精度のポリシー）

- **主指標は単位価格**: `価格 ÷ 内容量 × 100`。メーカー表記の内容量から計算する事実ベースの数値で、詰め替え・大容量も公平に比較できます。
- **1回あたりは「目安」**: `価格 ÷ total_shares`（標準使用量ベース）。商品ごとに使用量差があるため参考値として併記します。
- **ランキングは同一カテゴリ内**: g と ml は直接比較できないため、カテゴリ内で単価の安い順に並べます。
- **買い時判定は履歴が貯まってから**: 価格履歴が `MIN_HISTORY_DAYS`（既定5日）未満のあいだは断定せず「データ収集中」と表示します。

## 公開サイト

**https://minnaotokuni.github.io/detergent-price-compare/**

（GitHub Pages。`main` ブランチの `index.html` を配信）

## ローカルで価格を更新する

```bash
cp .env.example .env
# .env に RAKUTEN_APP_ID / RAKUTEN_ACCESS_KEY 等を設定

pip install -r requirements.txt
python main.py refresh   # 価格取得 → CSV追記 → index.html 再生成
```

更新後、変更を GitHub に push すると公開サイトも更新されます。

```bash
git add index.html price_history.csv  # price_history.csv は .gitignore 対象のため任意
git add index.html
git commit -m "Update prices"
git push
```

## 商品の追加・変更

`target_products.json` で各商品に以下を指定します。

- `rakuten_item_code`（例: `rakuten24:11398575`）… 価格は API で itemCode 指定のみ取得（曖昧検索は使わない）
- `size_value` / `size_unit`（例: `570` / `g`）… 単位価格 `価格 ÷ 内容量 × 100` の算出に使う【必須】
- `total_shares`（標準使用量での使用回数）＋ `dose_label`（例: `約10g/回`）… 1回あたり目安の算出・表示用
- `category_key` … 同一カテゴリ内ランキングの単位

## X (Twitter) 自動投稿

### 1. X Developer でアプリ作成

1. https://developer.x.com/ でプロジェクト作成（Free プラン可）
2. **Read and write** 権限を付与
3. **OAuth 1.0a** の Access Token / Secret を発行
4. `.env` に以下を貼り付け

```
X_API_KEY=...
X_API_SECRET=...
X_ACCESS_TOKEN=...
X_ACCESS_TOKEN_SECRET=...
SITE_URL=https://minnaotokuni.github.io/detergent-price-compare/
```

### 2. 動作確認（投稿しない）

```bash
# 1件だけプレビュー
python main.py post --target-id attack_zero_main --dry-run

# 朝7時・夜20時のまとめ2枠をプレビュー（LLMで140字文面・X投稿なし）
python main.py schedule --dry-run --no-fetch

# 7時 or 20時台だけまとめ1件をプレビュー
python main.py post-due --dry-run --no-fetch
```

**Ollama が起動していれば**（`ollama serve`）、まとめ投稿文はローカル LLM で無料生成されます（1日2回のみ）。クラウドは `OPENAI_API_KEY` 等が必要です。

### 3. 本番投稿

```bash
# 1件だけ実投稿（単品・ローカル文面）
python main.py post --target-id attack_zero_main

# 7時 or 20時枠の全商品まとめを1件投稿（cron 推奨）
python main.py post-due
```

### 4. 毎日自動化（Mac cron 例）

朝に価格更新、**7時と20時だけ**まとめ投稿:

```bash
crontab -e
```

```
0 6 * * * cd "/Users/watanabetakuya/Desktop/洗剤価格自動比較" && /usr/bin/python3 main.py refresh >> /tmp/detergent-refresh.log 2>&1
5 7 * * * cd "/Users/watanabetakuya/Desktop/洗剤価格自動比較" && /usr/bin/python3 main.py post-due >> /tmp/detergent-x.log 2>&1
5 20 * * * cd "/Users/watanabetakuya/Desktop/洗剤価格自動比較" && /usr/bin/python3 main.py post-due >> /tmp/detergent-x.log 2>&1
```

1日2枠（朝・夜）それぞれ全商品を凝縮した1ツイート。重複防止は `.post_log.json` の `digest_morning` / `digest_evening` キーで管理。

`schedule` は次の枠（7:00 or 20:00）まで待って1件投稿して終了します。常駐ループは使いません。

## ライセンス

個人利用・学習目的。アフィリエイトリンクを含みます。
