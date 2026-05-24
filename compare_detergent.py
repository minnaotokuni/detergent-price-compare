"""楽天市場の洗剤コスパ比較ツール（楽天API新仕様 v2026-04-01 対応）。

特徴:
    - 液体洗濯 / 粉末洗濯 / 食器用 / ボディソープ をカテゴリ切替で比較
    - 商品名から容量 (ml/L/g/kg) を正規表現で抽出。g↔ml はカテゴリ比重で換算
    - 本体 / 詰替 / その他 をキーワード近接法でセクション分けし、
      "本体 600ml + 詰替 1500ml" 等の組合せ商品も正確に総容量化
    - セット販売 (×3 / 3本セット 等) を検出して総容量に乗算
    - 地域別送料に対応 (--region と JSON 設定でショップ別×地域別のテーブル)
    - 楽天ポイント (pointRate) を実質価格に反映 (--include-points)
    - 楽天 API は メモリ + ファイル の二段キャッシュ (TTL 指定 / クリア対応)
    - アフィリエイトリンク出力 / CSV 一括出力

事前準備:
    1. https://webservice.rakuten.co.jp/app/list でアプリ登録し、
       applicationId と accessKey を取得 (アフィリエイトIDは任意)。
         ※ 2026-05-13 の API 全面刷新で accessKey が必須になりました。
         ※ 旧 app.rakuten.co.jp ドメインは完全停止しています。
    2. 依存導入: pip install -r requirements.txt
    3. 認証情報の設定 (どちらか1つ):
         (a) .env ファイル方式 (推奨):
             cp .env.example .env
             chmod 600 .env
             # .env の RAKUTEN_APP_ID= / RAKUTEN_ACCESS_KEY= の右側に貼り付け
             # スクリプト起動時に自動で読み込まれる
         (b) シェルの環境変数方式:
             export RAKUTEN_APP_ID="..."
             export RAKUTEN_ACCESS_KEY="..."        # 必須 (新仕様)
             export RAKUTEN_AFFILIATE_ID="..."     # 任意
             export RAKUTEN_REGION="kanto"           # 任意 (--region で上書き可)

使用例:
    # 液体洗濯洗剤のコスパ TOP5 を関東向け送料で表示
    python compare_detergent.py --region kanto

    # 食器用、各キーワード TOP3、詰替も含めて CSV 出力 + ポイント考慮
    python compare_detergent.py -c dish -n 3 --include-refill --include-points \\
        --csv result.csv

    # ショップ別×地域別送料テーブルを使う (shop_shipping.json 例は下記)
    python compare_detergent.py --shop-shipping shop_shipping.json --region okinawa

    # カテゴリ一覧 / キャッシュクリア
    python compare_detergent.py --list-categories
    python compare_detergent.py --clear-cache

shop_shipping.json 例:
    {
      "__default__": {
        "hokkaido": 1100, "tohoku": 800, "kanto": 600, "kinki": 700,
        "kyushu": 900, "okinawa": 1500, "default": 700
      },
      "rakuten24": 0,
      "kenko-com": { "default": 550 },
      "soukai": { "okinawa": 1200, "hokkaido": 900, "default": 500 }
    }
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional, Union

import requests

# .env を自動読込 (任意依存)。
# python-dotenv が無くても、シェルで export 済みの環境変数があれば動作する。
try:
    from dotenv import load_dotenv
    # スクリプトと同じディレクトリの .env を優先して読む
    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
    load_dotenv(override=False)  # カレントディレクトリの .env もフォールバックで読む
except ImportError:
    pass


# ===========================================================================
# Constants
# ===========================================================================

# 楽天 API 新仕様 (2026-04-01)。旧 app.rakuten.co.jp/services/api 系は
# 2026-05-13 で完全停止し、以下の新ドメイン/パスへ移行された。
RAKUTEN_API_URL = (
    "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260401"
)
# 新仕様のレート制限 (1リクエストあたりの最小間隔)。1.5秒以上が推奨。
DEFAULT_REQUEST_INTERVAL_SEC = 1.5
DEFAULT_CACHE_DIR = Path(".cache_rakuten")
DEFAULT_CACHE_TTL_SECONDS = 60 * 60  # 1 時間

# 配送地域 (送料テーブルのキー)
REGIONS: tuple[str, ...] = (
    "hokkaido",      # 北海道
    "tohoku",        # 東北 (青森・岩手・秋田・宮城・山形・福島)
    "kanto",         # 関東 (茨城・栃木・群馬・埼玉・千葉・東京・神奈川)
    "shinetsu",      # 信越 (新潟・長野)
    "hokuriku",      # 北陸 (富山・石川・福井)
    "tokai",         # 東海 (静岡・愛知・岐阜・三重)
    "kinki",         # 近畿 (滋賀・京都・大阪・兵庫・奈良・和歌山)
    "chugoku",       # 中国 (鳥取・島根・岡山・広島・山口)
    "shikoku",       # 四国 (徳島・香川・愛媛・高知)
    "kyushu",        # 九州 (福岡・佐賀・長崎・熊本・大分・宮崎・鹿児島)
    "okinawa",       # 沖縄
)

# JSON 内でショップ全体に適用したい既定値の特殊キー
GLOBAL_DEFAULT_SHOP_KEY = "__default__"

# 濃縮液体洗濯洗剤: 水30L〜45Lあたりのバイヤー基準使用量 (ml/g)
LAUNDRY_LIQUID_USE_ML_MIN = 10.0
LAUNDRY_LIQUID_USE_ML_MAX = 15.0
LAUNDRY_LIQUID_USE_ML_STANDARD = 12.5
PER_USE_WARN_THRESHOLD = 40.0


# ===========================================================================
# Category 定義
# ===========================================================================

@dataclass(frozen=True)
class Category:
    """比較対象カテゴリのメタ情報。"""

    key: str
    display_name: str
    density: float              # g/ml: g→ml 換算用の比重 (洗剤類は 1.0 前後)
    default_shipping_jpy: int   # 送料別×ショップ未設定時の最終フォールバック
    keywords: tuple[str, ...]   # 比較対象の検索キーワード
    base_use_ml_per_load: float # 標準的な「1回使用量」(ml)。
                                # 濃縮タイプはこれを倍率で割って補正する。
    use_unit_label: str         # 1回の表示単位 ("洗濯1回" "1食器洗い" 等)


# 各キーワードは実 API での "availability=1" 検索で
# 「本体表記&容量抽出可能」なヒット数が概ね5件以上得られるよう調整済み (2026-05時点)。
CATEGORIES: dict[str, Category] = {
    "laundry_liquid": Category(
        key="laundry_liquid",
        display_name="液体洗濯洗剤",
        density=1.05,
        default_shipping_jpy=500,
        keywords=(
            "アタックZERO 本体",      # 花王・主力液体
            "アリエール 洗剤",         # P&G (アリエールジェル/バイオサイエンス含む)
            "ナノックス NANOX",       # ライオン (主力 + NANOXone系)
            "ボールド 液体",           # P&G (ジェル + 液体)
            "ファーファ 液体洗剤",     # NSファーファ
        ),
        # 濃縮液体中心: 水30L洗濯 = 10〜15ml (バイヤー基準)。中央値12.5ml。
        # 濃縮倍率キーワードがあれば estimate_loads 内でさらに補正される。
        base_use_ml_per_load=LAUNDRY_LIQUID_USE_ML_STANDARD,
        use_unit_label="洗濯1回",
    ),
    "laundry_powder": Category(
        key="laundry_powder",
        display_name="粉末洗濯洗剤",
        density=0.80,                 # 嵩密度 (粉体)。あくまで比較指標
        default_shipping_jpy=500,
        keywords=(
            "アタック高活性バイオEX",  # 花王・粉末定番
            "アタック 粉末",           # 花王 (バイオパワー等)
            "ファーファ 粉末",         # 3倍濃縮を含む
            "アリエール 粉末",         # P&G 粉末
            "トップ 粉末",             # ライオン 粉末
        ),
        # 標準: 水30L洗濯あたり 30g ≒ 38ml (嵩密度 0.8)
        base_use_ml_per_load=38.0,
        use_unit_label="洗濯1回",
    ),
    "dish": Category(
        key="dish",
        display_name="食器用洗剤",
        density=1.03,
        default_shipping_jpy=400,
        keywords=(
            "ジョイ 食器用",           # P&G
            "キュキュット",            # 花王
            "チャーミーマジカ",        # ライオン
            "ヤシノミ洗剤",            # サラヤ
            "ファミリーフレッシュ",    # 花王プロ
        ),
        # 標準: 水1Lあたり 0.75ml 推奨 → 1回(食器10枚程度)で約 2ml
        base_use_ml_per_load=2.0,
        use_unit_label="食器10枚",
    ),
    "body_soap": Category(
        key="body_soap",
        display_name="ボディソープ",
        density=1.02,
        default_shipping_jpy=500,
        keywords=(
            "ビオレ ボディソープ",     # 花王 (ビオレu含む)
            "牛乳石鹸 ボディソープ",   # カウブランド
            "ナイーブ ボディソープ",   # クラシエ
            "ミノン 全身シャンプー",   # 第一三共ヘルスケア
            "DHC ボディソープ",        # DHC
        ),
        # ポンプ1プッシュ ≒ 3〜5ml、全身洗いで2プッシュ程度を想定
        base_use_ml_per_load=5.0,
        use_unit_label="1回シャワー",
    ),
    "fabric_softener": Category(
        key="fabric_softener",
        display_name="柔軟剤",
        density=1.0,
        default_shipping_jpy=500,
        keywords=(
            "レノア ハピネス",         # P&G
            "ソフラン アロマリッチ",   # ライオン
            "ランドリン",              # ボーテ
            "ケアプラス",              # 花王
            "ファブリーズ 柔軟剤",     # P&G
        ),
        # 標準: 水30L洗濯あたり 20ml
        base_use_ml_per_load=20.0,
        use_unit_label="洗濯1回",
    ),
    "bath_toilet": Category(
        key="bath_toilet",
        display_name="浴室・トイレ用洗剤",
        density=1.05,
        default_shipping_jpy=400,
        keywords=(
            "ルック バスタブ",         # ライオン
            "カビタン",                # 花王
            "トイレマジックリン",      # 花王
            "ジョイ トイレ",           # P&G
            "強力カビハイター",        # 花王
        ),
        # バス1回 / トイレ1回清掃の平均使用量
        base_use_ml_per_load=30.0,
        use_unit_label="1回清掃",
    ),
}


# ===========================================================================
# Regex
# ===========================================================================

# 容量: "800ml" / "1.5L" / "900g" / "1kg" / "2,000ml"
VOLUME_PATTERN = re.compile(
    r"(\d{1,3}(?:[,，]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(ml|mL|ML|ℓ|l|L|g|G|kg|KG|Kg)"
)

# セット個数:
#   "×3" / "x3" / "*3" / "✕3"
#   "3個" / "3本セット" / "3袋" / "3パック" など。
#   型番 (例: "AB-3 本体") を誤検知しないため、第2パターンは
#   「数字の直前が数字でもハイフンでもない」かつ「数字と単位の間にスペース無し」
#   を要求する。
SET_MULTIPLIER_PATTERNS = (
    re.compile(r"[×xX*✕]\s*(\d{1,2})(?!\d)"),
    re.compile(
        r"(?<![\d\-‐－])(\d{1,2})(?:個|本|袋|コ|箱|パック|セット|pcs?)(?:セット|入り|入)?",
        re.IGNORECASE,
    ),
)

# 本体 / 詰替 検出
REFILL_KEYWORDS = ("詰替", "詰め替え", "つめかえ", "詰替え", "リフィル", "refill")
MAIN_KEYWORDS = ("本体", "ボトル", "ポンプ")

# 容量とキーワードが「同一セクション」とみなせる最大文字数距離
SECTION_PROXIMITY_CHARS = 30

# 「○回分 / 約○○回 / ○○回洗える」などの使用回数表記
LOAD_COUNT_PATTERN = re.compile(
    r"(?:約\s*)?(\d{1,4})\s*回(?:分|洗い|洗濯|洗える|使用|シャワー|シャンプー)?"
)

# 「○倍濃縮 / ○倍洗浄 / ○.5倍」 などの倍率表記
CONCENTRATION_PATTERN = re.compile(r"(\d(?:\.\d)?)\s*倍\s*(?:濃縮|洗浄|の濃縮)?")

# キーワードベースで濃縮タイプを推定するフォールバック
HEAVY_CONCENTRATE_KEYWORDS = ("超濃縮", "ウルトラ濃縮", "超コンパクト", "ウルトラ")  # ~3 倍相当
MILD_CONCENTRATE_KEYWORDS = ("濃縮タイプ", "高濃縮")                              # ~2 倍相当
# 注: ジェルボールは下記 pod 検出側で個数=回数として扱うので倍率推定からは外す

# ジェルボール / タブレット製品 (個数 = 回数で換算する商品)
POD_PRODUCT_KEYWORDS = ("ジェルボール", "ジェル ボール", "ジェルポッド", "タブレット")
# 「11個」「12粒」「8入り」など (入浴剤の "○入り" を除外)
POD_COUNT_PATTERN = re.compile(
    r"(?<!\d)(\d{1,3})\s*(?:個入|粒入|個|粒|錠|ピース)(?!浴)"
)

# キャプションに頻出する「公式の1回使用量」を直接読み取るパターン群。
# 例: "水30Lに対して10g(10mL)が目安"
#     "目安量 25ml"
#     "1回あたり 8ml"
#     "1杯25ml"
#     "(キャップ)1杯 30g"
ML_PER_LOAD_PATTERNS = (
    # 水○Lに対して○g(○ml) ← 最も信頼度高い (g と ml が併記される)
    re.compile(
        r"水\s*\d{1,3}\s*L?\s*に対(?:し|して)?\s*\d{1,3}(?:\.\d)?\s*g\s*\(\s*(\d{1,3}(?:\.\d)?)\s*(?:mL|ml)\s*\)"
    ),
    # 「水30Lに10ml」「水30Lに25g」(単独表記)
    re.compile(
        r"水\s*\d{1,3}\s*L?\s*に(?:対(?:し|して))?\s*(\d{1,3}(?:\.\d)?)\s*(?:mL|ml|g)"
    ),
    # 「1回あたり ○ml」「1回 ○g」
    re.compile(r"1\s*回(?:あたり|につき)?\s*(\d{1,3}(?:\.\d)?)\s*(?:mL|ml|g)"),
    # 「目安(量) ○ml」「目安は ○g」
    re.compile(r"目安(?:量|は)?\s*(\d{1,3}(?:\.\d)?)\s*(?:mL|ml|g)"),
    # 「○mlが目安」「○gが目安」
    re.compile(r"(\d{1,3}(?:\.\d)?)\s*(?:mL|ml|g)\s*が目安"),
    # 「キャップ1杯 ○ml」「1杯25ml」
    re.compile(r"(?:キャップ\s*)?1\s*杯\s*(\d{1,3}(?:\.\d)?)\s*(?:mL|ml|g)"),
)


# ===========================================================================
# Models
# ===========================================================================

@dataclass
class Product:
    """正規化後の商品情報 (1 ヒット = 1 インスタンス)。"""

    keyword: str
    category: str
    item_name: str
    shop_name: str
    shop_code: str
    item_price: int
    postage_flag: int           # 0: 送料込/無料, 1: 送料別
    item_url: str
    affiliate_url: str

    # 容量内訳 (1セット分)
    main_volume_ml: float       # 本体側
    refill_volume_ml: float     # 詰替側
    other_volume_ml: float      # 区分不明 (キーワード近接無し)
    unit_volume_ml: float       # 1セット合計 (= main + refill + other)
    set_count: int              # 同梱本数
    volume_ml: float            # 総容量 = unit × set_count

    product_type: str           # "main" | "refill" | "unknown"

    # 使用量推定 (公平比較の核心)
    concentration_factor: float # 濃縮倍率 (1.0=通常, 2.0=2倍濃縮, 3.0=超濃縮等)
    use_per_load_ml: float      # この商品の1回使用量 (ml)
    load_count: float           # 推定使用回数 (volume_ml / use_per_load_ml)
    load_count_source: str      # "label" (商品名に明記) | "estimate" (倍率推定)

    # 価格
    region: str                 # 送料計算に使った地域 ("" = 未指定)
    shipping_fee: int           # 概算送料
    point_rate: int             # ポイント倍率 (1 = 1%)
    point_value: int            # ポイント円換算 (item_price × pointRate / 100)
    total_price: int            # 商品価格 + 送料
    effective_price: int        # 上記 - ポイント (--include-points 時のみ反映)
    price_per_load: float       # 【主指標】1回使用あたりの実質単価 (円)
    price_per_10ml: float       # 参考: 10ml あたり実質単価 (円)


# ===========================================================================
# Parsing
# ===========================================================================

def _to_ml(value: float, unit: str, density: float) -> float:
    """単位を ml に正規化する。g/kg はカテゴリ比重で割って換算。"""
    u = unit.lower().replace("ℓ", "l")
    if u == "ml":
        return value
    if u == "l":
        return value * 1000.0
    if u == "g":
        return value / density if density > 0 else value
    if u == "kg":
        return value * 1000.0 / density if density > 0 else value * 1000.0
    return 0.0


def extract_volume_by_section(name: str, density: float) -> dict[str, float]:
    """商品名を本体 / 詰替 / その他 のセクションに分けて容量を抽出する。

    アルゴリズム:
        1. 商品名中の本体/詰替キーワードの出現位置を全列挙
        2. 商品名中の容量トークン(ml/L/g/kg)の出現位置を全列挙
        3. 各容量について、最も近いキーワードのセクションに割り当てる
           (距離 ``SECTION_PROXIMITY_CHARS`` 以内、なければ "other")
        4. セクションごとに最大値を採用 (同一セクション内の "1.5L (1500ml)"
           のような重複表記を防ぐ)

    Returns:
        {"main": float, "refill": float, "other": float}  (単位: ml)
    """
    keyword_positions: list[tuple[int, str]] = []
    for kw in MAIN_KEYWORDS:
        for m in re.finditer(re.escape(kw), name):
            keyword_positions.append((m.start(), "main"))
    for kw in REFILL_KEYWORDS:
        for m in re.finditer(re.escape(kw), name, re.IGNORECASE):
            keyword_positions.append((m.start(), "refill"))

    by_section: dict[str, list[float]] = {"main": [], "refill": [], "other": []}
    for vmatch in VOLUME_PATTERN.finditer(name):
        try:
            value = float(vmatch.group(1).replace(",", "").replace("，", ""))
        except ValueError:
            continue
        ml = _to_ml(value, vmatch.group(2), density)
        if ml <= 0:
            continue

        vpos = vmatch.start()
        section = "other"
        nearest_dist: float = float("inf")
        for kp, ktype in keyword_positions:
            dist = abs(kp - vpos)
            if dist < nearest_dist and dist <= SECTION_PROXIMITY_CHARS:
                nearest_dist = dist
                section = ktype
        by_section[section].append(ml)

    return {
        "main": max(by_section["main"], default=0.0),
        "refill": max(by_section["refill"], default=0.0),
        "other": max(by_section["other"], default=0.0),
    }


def detect_set_multiplier(name: str) -> int:
    """同梱本数を検出。検出できなければ 1。

    2..30 の範囲外は誤検知とみなして 1 を返す (商品コード等の数字を弾く)。
    """
    for pattern in SET_MULTIPLIER_PATTERNS:
        for match in pattern.finditer(name):
            try:
                n = int(match.group(1))
            except ValueError:
                continue
            if 2 <= n <= 30:
                return n
    return 1


def detect_product_type(name: str) -> str:
    """商品名から 本体/詰替 を判定。

    本体・詰替の両方を含む「本体+詰替セット」は ``main`` として扱う
    (購買意思としては本体相当のため)。
    """
    lowered = name.lower()
    has_refill = any(kw.lower() in lowered for kw in REFILL_KEYWORDS)
    has_main = any(kw.lower() in lowered for kw in MAIN_KEYWORDS)
    if has_main:
        return "main"
    if has_refill:
        return "refill"
    return "unknown"


def detect_load_count(text: str) -> Optional[int]:
    """商品名/キャプションから「○回分」「約○○回」などを抽出する。

    2..2000 の範囲を妥当とみなす (商品コード等の数字を弾く)。
    """
    for match in LOAD_COUNT_PATTERN.finditer(text):
        try:
            n = int(match.group(1))
        except ValueError:
            continue
        if 2 <= n <= 2000:
            return n
    return None


def detect_pod_count(name: str, caption: str) -> Optional[int]:
    """ジェルボール/タブレット製品の個数を取得する。1個 = 1回として扱う。

    商品名 or キャプションにポッド系キーワードがあり、かつ個数表記が読み取れた
    時のみ値を返す。
    """
    text = f"{name} {caption}"
    if not any(kw in text for kw in POD_PRODUCT_KEYWORDS):
        return None
    for match in POD_COUNT_PATTERN.finditer(text):
        try:
            n = int(match.group(1))
        except ValueError:
            continue
        if 4 <= n <= 200:  # 4個未満はサンプル/景品の可能性、200個超は誤検知
            return n
    return None


def detect_ml_per_load(text: str) -> Optional[float]:
    """キャプション等から「公式の1回使用量 (ml or g)」を直接抽出する。

    g と ml は洗剤の比重が概ね 1 のため数値そのまま扱う (近似)。
    """
    for pattern in ML_PER_LOAD_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        try:
            value = float(match.group(1))
        except ValueError:
            continue
        # 妥当な範囲のみ採用 (0.5ml 〜 100ml/回)
        if 0.5 <= value <= 100.0:
            return value
    return None


def detect_concentration_factor(text: str) -> float:
    """商品名から濃縮倍率を推定する。デフォルト 1.0 (通常タイプ)。

    優先順位:
        1. "○倍濃縮" / "○倍洗浄" 等の明示的な倍率表記
        2. "超濃縮" "超コンパクト" "ウルトラ" → 3.0 倍
        3. "高濃縮" "濃縮タイプ" → 2.0 倍
        4. なければ 1.0
    """
    for match in CONCENTRATION_PATTERN.finditer(text):
        try:
            factor = float(match.group(1))
        except ValueError:
            continue
        if 1.0 <= factor <= 10.0:
            return factor
    for kw in HEAVY_CONCENTRATE_KEYWORDS:
        if kw in text:
            return 3.0
    for kw in MILD_CONCENTRATE_KEYWORDS:
        if kw in text:
            return 2.0
    return 1.0


def apply_laundry_liquid_load_guard(
    volume_ml: float,
    loads: float,
    upl: float,
    source: str,
) -> tuple[float, float, str]:
    """濃縮液体洗剤向け: 1回使用量が15ml超相当になる過小な回数推定を補正する。"""
    if volume_ml <= 0 or loads <= 0 or source == "pod":
        return loads, upl, source
    min_loads = volume_ml / LAUNDRY_LIQUID_USE_ML_MAX
    if loads >= min_loads:
        return loads, upl, source
    guarded_upl = LAUNDRY_LIQUID_USE_ML_STANDARD
    guarded_loads = volume_ml / guarded_upl
    return guarded_loads, guarded_upl, f"{source}_guarded"


def estimate_loads(
    volume_ml: float,
    name: str,
    caption: str,
    category: Category,
) -> tuple[float, float, float, str]:
    """総容量と商品テキストから、推定使用回数・1回使用量・濃縮倍率を返す。

    優先順位:
        1. **pod**       : ジェルボール/タブレット製品の個数 (最も正確)
        2. **caption_ml**: キャプションの「水○Lに○ml/g」「1回○ml」等の公式値
        3. **label**     : 商品名/キャプションの「約○○回分」表記
        4. **estimate**  : 「○倍濃縮」キーワードからカテゴリ標準を補正
        5. **default**   : カテゴリ標準値そのまま (上記すべて検出失敗)

    Returns:
        (load_count, use_per_load_ml, concentration_factor, source)
    """
    text = f"{name} {caption}"

    def finalize(
        loads: float, upl: float, factor: float, source: str
    ) -> tuple[float, float, float, str]:
        if category.key == "laundry_liquid":
            loads, upl, source = apply_laundry_liquid_load_guard(
                volume_ml, loads, upl, source
            )
        return loads, upl, factor, source

    # 1. ジェルボール製品: 個数 = 回数
    pod_count = detect_pod_count(name, caption)
    if pod_count:
        upl = volume_ml / pod_count if pod_count > 0 else 0.0
        # ジェルボールは「1粒で水30L」相当なので便宜的に factor=2.0
        return finalize(float(pod_count), upl, 2.0, "pod")

    # 2. キャプションから「1回あたり○ml」を直接抽出 (公式値)
    direct_ml = detect_ml_per_load(text)
    if direct_ml and direct_ml > 0:
        loads = volume_ml / direct_ml
        factor = category.base_use_ml_per_load / direct_ml if direct_ml > 0 else 1.0
        return finalize(loads, direct_ml, factor, "caption_ml")

    # 3. 「○回分」表記
    label_loads = detect_load_count(text)
    if label_loads:
        upl = volume_ml / label_loads if label_loads > 0 else 0.0
        factor = (category.base_use_ml_per_load / upl) if upl > 0 else 1.0
        return finalize(float(label_loads), upl, factor, "label")

    # 4. 濃縮倍率推定
    factor = detect_concentration_factor(text)
    if factor > 1.0:
        upl = category.base_use_ml_per_load / factor
        loads = volume_ml / upl if upl > 0 else 0.0
        return finalize(loads, upl, factor, "estimate")

    # 5. デフォルト: カテゴリ標準
    upl = category.base_use_ml_per_load
    loads = volume_ml / upl if upl > 0 else 0.0
    return finalize(loads, upl, 1.0, "default")


# ===========================================================================
# Shipping (region aware)
# ===========================================================================

# JSON エントリ型: int (全国一律) または dict (地域別)
ShippingEntry = Union[int, dict]


def _resolve_shipping_entry(entry: ShippingEntry, region: str) -> Optional[int]:
    """エントリを地域指定で解決して送料 (円) を返す。解決不能なら None。"""
    if isinstance(entry, (int, float)):
        return int(entry)
    if isinstance(entry, dict):
        if region and region in entry:
            return int(entry[region])
        if "default" in entry:
            return int(entry["default"])
    return None


def estimate_shipping(
    postage_flag: int,
    shop_code: str,
    region: str,
    category: Category,
    shop_overrides: dict[str, ShippingEntry],
) -> int:
    """送料を概算する。フォールバック順序は以下のとおり。

        1. postageFlag == 0 → 送料込み/無料: 0 円
        2. shop_overrides[shop_code] を地域で解決
        3. shop_overrides["__default__"] を地域で解決 (全ショップ共通の既定)
        4. category.default_shipping_jpy (最終フォールバック)
    """
    if postage_flag == 0:
        return 0

    if shop_code in shop_overrides:
        fee = _resolve_shipping_entry(shop_overrides[shop_code], region)
        if fee is not None:
            return fee

    if GLOBAL_DEFAULT_SHOP_KEY in shop_overrides:
        fee = _resolve_shipping_entry(
            shop_overrides[GLOBAL_DEFAULT_SHOP_KEY], region
        )
        if fee is not None:
            return fee

    return category.default_shipping_jpy


# ===========================================================================
# Point (rakuten point) calculation
# ===========================================================================

def calc_point_value(item_price: int, point_rate: int) -> int:
    """楽天ポイントを円換算する (1 倍 = 1%)。

    送料には倍率が掛からない前提 (実運用上もそれが標準)。
    """
    if point_rate <= 0:
        return 0
    return int(item_price * point_rate / 100)


# ===========================================================================
# Rakuten API client (memory + file cache)
# ===========================================================================

class RakutenClient:
    """楽天市場 商品検索API ラッパー (新仕様 v2026-04-01 対応)。"""

    def __init__(
        self,
        app_id: str,
        access_key: str,
        affiliate_id: str = "",
        referer: str = "",
        cache_dir: Optional[Path] = DEFAULT_CACHE_DIR,
        cache_ttl: int = DEFAULT_CACHE_TTL_SECONDS,
        request_interval_sec: float = DEFAULT_REQUEST_INTERVAL_SEC,
    ) -> None:
        if not app_id:
            raise ValueError("applicationId は必須です (RAKUTEN_APP_ID)")
        if not access_key:
            raise ValueError("accessKey は必須です (RAKUTEN_ACCESS_KEY)")
        self.app_id = app_id
        self.access_key = access_key
        self.affiliate_id = affiliate_id
        # 新仕様: アプリ登録時に「許可されたウェブサイト」に登録したURLを
        # Referer ヘッダーとして送る必要がある (Webアプリケーションタイプの場合)。
        self.referer = referer
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.cache_ttl = cache_ttl
        self.request_interval_sec = request_interval_sec
        self._memory_cache: dict[str, list[dict]] = {}
        self._last_request_at: float = 0.0
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _cache_key(params: dict) -> str:
        canonical = json.dumps(params, sort_keys=True, ensure_ascii=False)
        return hashlib.sha1(canonical.encode("utf-8")).hexdigest()

    def _read_file_cache(self, key: str) -> Optional[list[dict]]:
        if not self.cache_dir:
            return None
        path = self.cache_dir / f"{key}.json"
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if time.time() - payload.get("fetched_at", 0) > self.cache_ttl:
            return None
        return payload.get("items")

    def _write_file_cache(self, key: str, items: list[dict]) -> None:
        if not self.cache_dir:
            return
        path = self.cache_dir / f"{key}.json"
        payload = {"fetched_at": time.time(), "items": items}
        try:
            path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request_at
        if 0 < elapsed < self.request_interval_sec:
            time.sleep(self.request_interval_sec - elapsed)

    def search(
        self,
        keyword: str = "",
        hits: int = 30,
        postage_flag: Optional[int] = None,
        item_code: str = "",
        shop_code: str = "",
    ) -> list[dict]:
        """新仕様で検索。formatVersion=2 を使い items[i].xxx の平坦形式で受ける。

        item_code を渡した場合は ``itemCode`` パラメタで API をピンポイント呼び出し
        (キーワード検索を経由せず特定商品の現在価格を直接取得)。
        """
        if not keyword and not item_code:
            raise ValueError("keyword か item_code のどちらかが必須です")
        params: dict[str, Union[str, int]] = {
            "applicationId": self.app_id,
            "accessKey": self.access_key,
            "hits": hits,
            "format": "json",
            "formatVersion": 2,
        }
        if item_code:
            params["itemCode"] = item_code
        else:
            params["keyword"] = keyword
            params["sort"] = "+itemPrice"
            params["imageFlag"] = 1
            params["availability"] = 1
        if shop_code:
            params["shopCode"] = shop_code
        if postage_flag is not None:
            params["postageFlag"] = postage_flag
        if self.affiliate_id:
            params["affiliateId"] = self.affiliate_id

        # キャッシュキーは認証情報を除外して安定化
        cache_params = {
            k: v for k, v in params.items()
            if k not in ("applicationId", "accessKey", "affiliateId")
        }
        key = self._cache_key(cache_params)

        if key in self._memory_cache:
            return self._memory_cache[key]

        cached = self._read_file_cache(key)
        if cached is not None:
            self._memory_cache[key] = cached
            return cached

        headers: dict[str, str] = {}
        if self.referer:
            # 楽天 v2026-04-01 は登録ドメインからの Referer 送信を要求する。
            # スキームが無い場合は https:// を自動補完 (requests がヘッダーとして
            # 受け付けても、サーバー側で無効と判定されるケースを防ぐ)。
            ref = self.referer
            if not ref.startswith(("http://", "https://")):
                ref = "https://" + ref.lstrip("/")
            headers["Referer"] = ref
            headers["Origin"] = ref.rstrip("/")

        self._throttle()
        resp = requests.get(
            RAKUTEN_API_URL, params=params, headers=headers, timeout=10
        )
        self._last_request_at = time.time()
        resp.raise_for_status()
        # 新APIは formatVersion=2 を指定しても "Items" (大文字) で返るケースあり。
        # 旧仕様 "items" / "Items" の両方をフォールバックで受ける。
        payload = resp.json()
        items = payload.get("items") or payload.get("Items") or []
        self._memory_cache[key] = items
        self._write_file_cache(key, items)
        return items


# ===========================================================================
# Ranking
# ===========================================================================

def build_products(
    keyword: str,
    category: Category,
    raw_items: list[dict],
    shop_overrides: dict[str, ShippingEntry],
    region: str,
    include_refill: bool,
    include_points: bool,
) -> list[Product]:
    """API レスポンスを Product のリストに正規化し、コスパ昇順に並べる。

    formatVersion=2 のため item は平坦オブジェクト (items[i].xxx)。
    旧 formatVersion=1 形式 (items[i].item.xxx) もフォールバックで吸収する。

    並び順は **price_per_load (1回使用あたり実質単価)** の昇順。
    濃縮タイプも通常タイプも公平に比較できる。
    """
    results: list[Product] = []
    for entry in raw_items:
        # 新仕様 (formatVersion=2): entry がそのまま商品。
        # 旧仕様互換: entry が {"Item": {...}} ならアンラップ。
        item = entry.get("Item", entry) if isinstance(entry, dict) else {}
        name = item.get("itemName", "")
        caption = item.get("itemCaption", "") or ""

        product_type = detect_product_type(name)
        if not include_refill and product_type == "refill":
            continue

        sections = extract_volume_by_section(name, category.density)
        unit_volume_ml = sections["main"] + sections["refill"] + sections["other"]
        if unit_volume_ml <= 0:
            continue

        set_count = detect_set_multiplier(name)
        total_volume_ml = unit_volume_ml * set_count

        item_price = int(item.get("itemPrice", 0))
        if item_price <= 0:
            continue
        shop_code = item.get("shopCode", "")
        postage_flag = int(item.get("postageFlag", 0))
        shipping_fee = estimate_shipping(
            postage_flag, shop_code, region, category, shop_overrides
        )
        total_price = item_price + shipping_fee

        point_rate = int(item.get("pointRate", 1) or 0)
        point_value = calc_point_value(item_price, point_rate)
        effective_price = total_price - point_value if include_points else total_price

        # 使用回数推定
        loads, use_per_load, factor, src = estimate_loads(
            total_volume_ml, name, caption, category
        )
        if loads <= 0:
            continue
        price_per_load = round(effective_price / loads, 2)

        results.append(
            Product(
                keyword=keyword,
                category=category.key,
                item_name=name,
                shop_name=item.get("shopName", ""),
                shop_code=shop_code,
                item_price=item_price,
                postage_flag=postage_flag,
                item_url=item.get("itemUrl", ""),
                affiliate_url=item.get("affiliateUrl") or item.get("itemUrl", ""),
                main_volume_ml=sections["main"],
                refill_volume_ml=sections["refill"],
                other_volume_ml=sections["other"],
                unit_volume_ml=unit_volume_ml,
                set_count=set_count,
                volume_ml=total_volume_ml,
                product_type=product_type,
                concentration_factor=round(factor, 2),
                use_per_load_ml=round(use_per_load, 2),
                load_count=round(loads, 1),
                load_count_source=src,
                region=region,
                shipping_fee=shipping_fee,
                point_rate=point_rate,
                point_value=point_value,
                total_price=total_price,
                effective_price=effective_price,
                price_per_load=price_per_load,
                price_per_10ml=round(effective_price / total_volume_ml * 10, 2),
            )
        )
    results.sort(key=lambda p: p.price_per_load)
    return results


def print_ranking(
    keyword: str,
    category: Category,
    products: list[Product],
    top_n: int,
    include_points: bool,
) -> None:
    print(f"\n=== [{category.display_name}] {keyword} のコスパランキング ===")
    if not products:
        print("  容量を抽出できる対象商品が見つかりませんでした。")
        return

    type_label = {"main": "本体", "refill": "詰替", "unknown": "種別不明"}
    for rank, p in enumerate(products[:top_n], start=1):
        postage = (
            "送料込み" if p.postage_flag == 0
            else f"送料別(+概算¥{p.shipping_fee:,}/{p.region or '地域未指定'})"
        )
        set_label = f"×{p.set_count}セット" if p.set_count > 1 else "単品"

        # 容量内訳の表示 (有意な側だけ並べる)
        vol_parts: list[str] = []
        if p.main_volume_ml > 0:
            vol_parts.append(f"本体{p.main_volume_ml:.0f}ml")
        if p.refill_volume_ml > 0:
            vol_parts.append(f"詰替{p.refill_volume_ml:.0f}ml")
        if p.other_volume_ml > 0:
            vol_parts.append(f"その他{p.other_volume_ml:.0f}ml")
        vol_breakdown = " + ".join(vol_parts) if vol_parts else f"{p.unit_volume_ml:.0f}ml"

        # 使用量推定の根拠
        load_src = {
            "pod":        f"ジェルボール {p.load_count:.0f}個=回",
            "caption_ml": "公式キャプション値",
            "label":      "商品名/説明に明記",
            "estimate":   f"濃縮{p.concentration_factor:.1f}倍補正",
            "total_shares": "target_products.json 固定回数",
            "default":    "カテゴリ標準値",
        }.get(p.load_count_source, p.load_count_source)

        print(f"[{rank}] ¥{p.price_per_load:.2f} / {category.use_unit_label}"
              f"  (約{p.load_count:.0f}回分・参考¥{p.price_per_10ml:.2f}/10ml)")
        print(f"    商品 : {p.item_name[:70]}")
        print(f"    種別 : {type_label.get(p.product_type, '?')} / {set_label}"
              f" / {load_src} (1回 {p.use_per_load_ml:.1f}ml)")
        print(f"    容量 : {vol_breakdown} = {p.unit_volume_ml:.0f}ml × {p.set_count}"
              f" = {p.volume_ml:.0f}ml")
        price_line = f"    価格 : ¥{p.item_price:,} ({postage}) → 実質 ¥{p.total_price:,}"
        if include_points and p.point_value > 0:
            price_line += (
                f" − ポイント ¥{p.point_value:,} (×{p.point_rate})"
                f" = ¥{p.effective_price:,}"
            )
        print(price_line)
        print(f"    店舗 : {p.shop_name}")
        print(f"    URL  : {p.affiliate_url}")


def make_group_key(p: Product) -> tuple:
    """同一商品を判定するための正規化キー。

    粒度: (検索キーワード=ブランド, 50ml刻みの容量バケット, 本体/詰替, セット数)
    別ショップ・別香り・別パッケージは同じグループに集約され、最安1件のみ代表させる。
    """
    bucket_ml = int(round(p.volume_ml / 50.0) * 50)
    return (p.keyword, bucket_ml, p.product_type, p.set_count)


def pick_cheapest_per_group(products: list[Product]) -> list[Product]:
    """同一商品グループ内で price_per_load 最小の1件だけを残してソート済みリストを返す。"""
    representatives: dict[tuple, Product] = {}
    for p in products:
        key = make_group_key(p)
        current = representatives.get(key)
        if current is None or p.price_per_load < current.price_per_load:
            representatives[key] = p
    return sorted(representatives.values(), key=lambda x: x.price_per_load)


# ===========================================================================
# X (Twitter) 投稿テンプレート
# ===========================================================================

CATEGORY_HASHTAGS = {
    "laundry_liquid": "#液体洗濯洗剤",
    "laundry_powder": "#粉末洗濯洗剤",
    "dish":           "#食器用洗剤",
    "body_soap":      "#ボディソープ",
    "fabric_softener": "#柔軟剤",
    "bath_toilet":    "#浴室トイレ洗剤",
}


def format_x_post(p: Product, category: Category) -> str:
    """X (Twitter) ポスト用の整形済み文字列を返す。

    - 文字数は概ね 220 字以内 (URL は t.co 短縮で 23 字換算)
    - アフィリエイト URL は楽天のアフィリエイトリンクをそのまま貼る
    - ハッシュタグ末尾の #PR はステマ規制対策 (景品表示法)
    """
    type_label = {"main": "本体", "refill": "詰替", "unknown": ""}.get(p.product_type, "")
    set_label = f"×{p.set_count}セット" if p.set_count > 1 else ""
    vol_str = f"{p.volume_ml:.0f}ml"
    hashtag = CATEGORY_HASHTAGS.get(category.key, "")

    lines = [
        f"【楽天最安】{p.keyword}",
        f"💰 {category.use_unit_label} ¥{p.price_per_load:.1f}"
        f"（{p.load_count:.0f}回分・実質¥{p.total_price:,}）",
        f"📦 {vol_str} {type_label}{set_label}".strip(),
        f"🏪 {p.shop_name[:25]}",
        p.affiliate_url,
        f"#コスパ比較 {hashtag} #節約 #PR",
    ]
    return "\n".join(lines)


def x_post_char_count(text: str) -> int:
    """X (Twitter) で実際にカウントされる文字数を概算する。

    X は URL を t.co 短縮の 23 文字としてカウントする (実 URL 長は無視)。
    """
    url_pattern = re.compile(r"https?://\S+")
    urls = url_pattern.findall(text)
    text_without_urls = url_pattern.sub("", text)
    return len(text_without_urls) + 23 * len(urls)


def print_x_posts(
    keyword: str,
    category: Category,
    products: list[Product],
    top_n: int,
) -> None:
    """X 投稿テンプレートを区切り線付きで出力する。"""
    if not products:
        return
    print(f"\n############### [{category.display_name}] {keyword} ###############")
    for rank, p in enumerate(products[:top_n], start=1):
        post = format_x_post(p, category)
        x_chars = x_post_char_count(post)
        status = "✓" if x_chars <= 280 else f"✗超過({x_chars - 280}字オーバー)"
        print(f"\n────────── 投稿 #{rank}  X実カウント {x_chars}/280字 {status} ──────────")
        print(post)


def write_csv(path: Path, products: list[Product]) -> None:
    """全結果を CSV (UTF-8 BOM 付き) で書き出す。"""
    if not products:
        return
    fields = list(asdict(products[0]).keys())
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for p in products:
            writer.writerow(asdict(p))


# ===========================================================================
# CLI
# ===========================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="楽天市場の洗剤コスパ比較ツール (地域別送料 / ポイント考慮対応)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-c", "--category",
        default="laundry_liquid",
        choices=list(CATEGORIES.keys()),
        help="比較対象カテゴリ (default: laundry_liquid)",
    )
    parser.add_argument(
        "-n", "--top", type=int, default=5,
        help="各キーワードでの表示件数 (default: 5)",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("RAKUTEN_REGION", ""),
        choices=("", *REGIONS),
        help=("配送地域。送料テーブルがある場合に参照される。"
              " 環境変数 RAKUTEN_REGION でも指定可"),
    )
    parser.add_argument(
        "--include-refill", action="store_true",
        help="詰替「のみ」の商品も比較対象に含める (本体+詰替セットは常に対象)",
    )
    parser.add_argument(
        "--include-points", action="store_true",
        help="楽天ポイントを実質価格から差し引く (1倍=1%%)",
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="API キャッシュを使わず毎回取得",
    )
    parser.add_argument(
        "--cache-ttl", type=int, default=DEFAULT_CACHE_TTL_SECONDS,
        help=f"キャッシュ有効期間(秒) (default: {DEFAULT_CACHE_TTL_SECONDS})",
    )
    parser.add_argument(
        "--clear-cache", action="store_true",
        help=f"{DEFAULT_CACHE_DIR} を削除して終了",
    )
    parser.add_argument(
        "--list-categories", action="store_true",
        help="利用可能なカテゴリ一覧を表示して終了",
    )
    parser.add_argument(
        "--csv", type=Path, default=None,
        help="全結果を CSV に書き出すパス",
    )
    parser.add_argument(
        "--no-group", action="store_true",
        help="同一商品の集約を無効化 (デフォルトは集約: 別ショップ・別香りを1つにまとめ最安代表)",
    )
    parser.add_argument(
        "--x-post", action="store_true",
        help="X (Twitter) にそのまま貼れる投稿テンプレート形式で出力 (アフィリエイトURL付き)",
    )
    parser.add_argument(
        "--shop-shipping", type=Path, default=None,
        help=("ショップ別送料の上書き JSON (int または "
              '{"region名": 円, "default": 円} のネスト構造)'),
    )
    parser.add_argument(
        "--hits", type=int, default=30,
        help="各キーワードで取得する商品数 (max 30, default: 30)",
    )
    return parser.parse_args()


def load_shop_overrides(path: Optional[Path]) -> dict[str, ShippingEntry]:
    """ショップ別送料 JSON を読み込む。

    エントリは ``int`` か、``{"region": 円, "default": 円}`` 形式の dict。
    両方を許容する。
    """
    if not path:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[WARN] ショップ送料表の読込失敗: {e}")
        return {}

    overrides: dict[str, ShippingEntry] = {}
    for shop, entry in data.items():
        if isinstance(entry, (int, float)):
            overrides[str(shop)] = int(entry)
        elif isinstance(entry, dict):
            # 値を int に正規化
            cleaned: dict[str, int] = {}
            for k, v in entry.items():
                try:
                    cleaned[str(k)] = int(v)
                except (TypeError, ValueError):
                    print(f"[WARN] {shop}.{k} は数値ではないため無視: {v!r}")
            overrides[str(shop)] = cleaned
        else:
            print(f"[WARN] {shop} のエントリ形式が不正のため無視: {entry!r}")
    return overrides


def print_categories() -> None:
    print("利用可能なカテゴリ:")
    for key, cat in CATEGORIES.items():
        print(f"  {key:<16s} {cat.display_name}  (比重={cat.density},"
              f" 既定送料=¥{cat.default_shipping_jpy})")
        for kw in cat.keywords:
            print(f"      - {kw}")


def clear_cache(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
        print(f"キャッシュを削除しました: {path}")
    else:
        print(f"キャッシュは存在しません: {path}")


def main() -> None:
    args = parse_args()

    if args.list_categories:
        print_categories()
        return

    if args.clear_cache:
        clear_cache(DEFAULT_CACHE_DIR)
        return

    app_id = os.environ.get("RAKUTEN_APP_ID", "").strip()
    access_key = os.environ.get("RAKUTEN_ACCESS_KEY", "").strip()
    affiliate_id = os.environ.get("RAKUTEN_AFFILIATE_ID", "").strip()
    referer = os.environ.get("RAKUTEN_REFERER", "").strip()
    missing: list[str] = []
    if not app_id:
        missing.append("RAKUTEN_APP_ID")
    if not access_key:
        missing.append("RAKUTEN_ACCESS_KEY")
    if missing:
        raise SystemExit(
            "環境変数 " + " / ".join(missing) + " が未設定です。\n"
            "楽天APIは 2026-05-13 の仕様変更で applicationId と accessKey の両方が必須になりました。\n"
            " https://webservice.rakuten.co.jp/app/list で確認して .env に貼り付けてください。"
        )

    category = CATEGORIES[args.category]
    shop_overrides = load_shop_overrides(args.shop_shipping)
    client = RakutenClient(
        app_id=app_id,
        access_key=access_key,
        affiliate_id=affiliate_id,
        referer=referer,
        cache_dir=None if args.no_cache else DEFAULT_CACHE_DIR,
        cache_ttl=args.cache_ttl,
    )

    region = args.region or ""
    if region:
        print(f"[INFO] 配送地域: {region}")
    if args.include_points:
        print("[INFO] 楽天ポイントを実質価格に反映します (1倍=1%)")

    all_products: list[Product] = []
    for keyword in category.keywords:
        try:
            raw_items = client.search(keyword, hits=min(args.hits, 30))
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            body = e.response.text[:120] if e.response is not None else ""
            print(f"[ERROR] {keyword}: HTTP {status} {body}")
            continue
        except requests.RequestException as e:
            print(f"[ERROR] {keyword}: 通信失敗 {e}")
            continue

        products = build_products(
            keyword=keyword,
            category=category,
            raw_items=raw_items,
            shop_overrides=shop_overrides,
            region=region,
            include_refill=args.include_refill,
            include_points=args.include_points,
        )

        # 同一商品集約 (デフォルト ON)。--no-group で無効化可。
        if not args.no_group:
            products = pick_cheapest_per_group(products)

        if args.x_post:
            print_x_posts(keyword, category, products, args.top)
        else:
            print_ranking(keyword, category, products, args.top, args.include_points)
        all_products.extend(products)

    if args.csv:
        write_csv(args.csv, sorted(all_products, key=lambda p: p.price_per_load))
        print(f"\nCSV出力: {args.csv} ({len(all_products)}件)")


if __name__ == "__main__":
    main()
