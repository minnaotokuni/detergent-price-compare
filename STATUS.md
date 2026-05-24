# Detergent Bot v2.0 — 状態記録（2026-05-24）

## 概要

楽天24・サンドラッグ・爽快ドラッグの3店舗から洗剤・日用品価格を取得し、**1回あたり実質単価**で比較するバイヤー向けサイト。楽天24の3980円送料無料ラインを軸にまとめ買いを促進する。

## 完了済み機能

### サイト（index.html）

- 楽天24ヒーローカード（1回あたり単価・買い時シグナル）
- 3店舗比較（空欄時は代替候補で補完）
- カテゴリタブ：すべて / 洗濯洗剤 / 柔軟剤 / 食器洗剤 / お風呂・トイレ / ハンド・ボディ
- 下部固定：「390円均一・調整用お宝リスト」（送料無料ライン調整用）
- バイヤー向けアドバイスヒーロー（赤グラデーション）

### 1回あたり単価ロジック（v2.0 核心）

1. **total_shares 最優先**（`target_products.json`）
   - 洗濯液体8品 + 粉末3品に固定洗濯回数を定義
   - 指定時は容量自動判別をスキップ → `販売価格 ÷ total_shares`
   - CSVの古い異常値も `enrich_offer` で上書き

2. **自動判別セーフティガード**（`compare_detergent.py`）
   - 液体洗濯の標準使用量：25ml → **12.5ml**（10〜15ml中央）
   - `apply_laundry_liquid_load_guard()`：回数過小評価を補正

3. **異常値警告**
   - 1回40円超 → stderr に `[WARN]` 出力

### 検索・マッチング

- JANコード優先照合 + キーワード二段構え
- バリアント除外（ドラム専用、部屋干し、セット、ワンパック等）
- 店舗ID固定（楽天24 / サンドラッグ / 爽快ドラッグ）

### 買い時判定・X投稿

- 楽天24の1回あたり単価履歴ベース（過去最安 / 平均以下 / 高値）
- `cmd_schedule` + tweepy 自動投稿（既存ロジック維持）
- `price_history.csv` 追記ロジック維持

## 商品数

- **22商品**（`target_products.json`）
- カテゴリ内訳：洗濯液体8 / 粉末3 / 柔軟剤3 / 食器4 / お風呂2 / ボディ2

## total_shares 設定済み（洗濯11品）

| ID | 商品 | total_shares |
|----|------|-------------|
| attack_zero_main | アタックZERO 本体 | 62 |
| attack_zero_refill | アタックZERO 詰替大 | 180 |
| nanox_one_main | ナノックスone 本体 | 62 |
| nanox_one_refill | NANOX one 詰替 | 120 |
| ariel_jel_main | アリエール ジェル 本体 | 55 |
| ariel_bio_refill | アリエール バイオサイエンス 詰替 | 130 |
| bold_jel_main | ボールド ジェル 本体 | 55 |
| fafa_liquid | ファーファ フリー& 液体 | 50 |
| attack_bio_powder | アタック 高活性バイオEX 粉末 | 40 |
| ariel_powder | アリエール サイエンスプラス 粉末 | 45 |
| top_powder | トップ ハイジア 粉末 | 45 |

## 修正効果（2026-05-24 CSV + total_shares 適用後）

| 商品 | 修正前（例） | 修正後（例） |
|------|-------------|-------------|
| アリエール ジェル | 41.2円/回 | **10.9円/回**（598÷55） |
| ボールド ジェル | 40.1円/回 | **9.8円/回**（539÷55） |
| アタックZERO 詰替 | — | **14.9円/回** |

## 既知の未解決課題

| 課題 | 状態 |
|------|------|
| 食器・お風呂の高単価（132円、274円等） | total_shares 未設定、CSV古値 or 誤商品 |
| NANOX one 詰替 | 厳格フィルタ後に3店舗全滅することがある |
| 楽天24で高単価SKUヒット | 価格自体が高い場合、total_shares でも高く見える |
| APIにJAN不在 | キーワードマッチに依存 |
| スライド記載の「30円超→大容量切替」 | **未実装**（現状は40円警告 + total_shares） |

## ファイル構成

```
main.py                 … 統合（取得/CSV/HTML/X）
compare_detergent.py    … 容量・回数推定エンジン
target_products.json    … 監視商品マスタ
price_history.csv       … 日次価格履歴
index.html              … 生成サイト
.env                    … APIキー（gitignore）
roadmap-slides.html     … Strategic Roadmap スライド
```

## 次フェーズ（Roadmap 参照）

- Part 1 残：食器/お風呂への total_shares 追加、JANヒット率向上
- Part 2：3980円ライン訴求の強化（お宝リスト拡充）
- Part 3：Amazon PA-API、カテゴリ拡張（Health/Grocery/Baby）
