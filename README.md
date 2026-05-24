# 日用品まとめ買い トータルコスト最適化

楽天24を軸に、洗剤・柔軟剤・食器用洗剤などの**1回あたり真の単価**を比較するサイトです。

## 公開サイト

**https://takuya-watanabeaaa.github.io/detergent-price-compare/**

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

`target_products.json` で各商品の `rakuten_item_code`（例: `rakuten24:11398575`）と `total_shares`（総使用回数）を指定します。価格は API で itemCode 指定のみ取得し、単価は `価格 ÷ total_shares` で計算します。

## ライセンス

個人利用・学習目的。アフィリエイトリンクを含みます。
