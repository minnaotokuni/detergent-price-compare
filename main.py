"""価格履歴CSV・買い時判定・HTML生成・X投稿の統合スクリプト。"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

from compare_detergent import (
    CATEGORIES,
    PER_USE_WARN_THRESHOLD,
)

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
    load_dotenv(override=False)
except ImportError:
    pass

try:
    import tweepy
except ImportError:
    tweepy = None


BASE_DIR = Path(__file__).resolve().parent
TARGET_PRODUCTS_PATH = BASE_DIR / "target_products.json"
PRICE_HISTORY_CSV = BASE_DIR / "price_history.csv"
DEFAULT_HTML_PATH = BASE_DIR / "index.html"
DEFAULT_POST_LOG = BASE_DIR / ".post_log.json"
RAKUTEN_API_URL = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260401"
X_CHAR_LIMIT = 140

# 1日2回のみ: (slot_id, 表示名, 実行時刻の時)
SCHEDULE_SLOTS: tuple[tuple[str, str, int], ...] = (
    ("morning", "朝7時", 7),
    ("evening", "夜20時", 20),
)

DIGEST_DEFAULT_HASHTAGS = "#日用品 #節約 #PR"

DIGEST_LLM_INSTRUCTION = (
    "以下の入力データ（各商品の楽天24価格と買い時判定）を元に、"
    "主婦・主夫層が『楽天24でまとめ買いして送料無料ライン（3980円）を突破したくなる』ような、"
    "バイヤー推奨の仕入れ速報ツイートの【本文】を書いてください（140文字以内・厳守）。"
    "トーン例: 「楽天24で洗剤まとめ買いしてる主婦・主夫さん、必見！アタックZERO激アツ最安値！"
    "送料無料ライン超え間違いなし！他の商品も激安価格♪」のように、送料無料ライン・激アツ商品名を具体的に。"
    "ハッシュタグとURLはシステムが末尾に付けるので本文には書かない。"
    "出力は投稿本文のみ。余計な説明・引用符は不要。"
)

CSV_COLUMNS = [
    "日付",
    "JANコード",
    "商品名",
    "楽天24価格",
    "サンドラッグ価格",
    "爽快ドラッグ価格",
    "楽天24_1回あたり",
    "サンドラッグ_1回あたり",
    "爽快ドラッグ_1回あたり",
    "最安_1回あたり",
]


@dataclass(frozen=True)
class ShopConfig:
    key: str
    label: str
    csv_column: str
    per_use_csv_column: str
    shop_codes: tuple[str, ...]
    match_terms: tuple[str, ...]
    button_class: str


SHOPS = [
    ShopConfig(
        "rakuten24",
        "楽天24",
        "楽天24価格",
        "楽天24_1回あたり",
        ("rakuten24",),
        ("楽天24", "rakuten24"),
        "bg-red-600",
    ),
    ShopConfig(
        "sundrug",
        "サンドラッグ",
        "サンドラッグ価格",
        "サンドラッグ_1回あたり",
        ("sundrug",),
        ("サンドラッグ", "sundrug"),
        "bg-emerald-600",
    ),
    ShopConfig(
        "soukai",
        "爽快ドラッグ",
        "爽快ドラッグ価格",
        "爽快ドラッグ_1回あたり",
        ("soukai",),
        ("爽快ドラッグ", "soukai"),
        "bg-sky-600",
    ),
]
PER_USE_BEST_COLUMN = "最安_1回あたり"
RAKUTEN24 = SHOPS[0]
RAKUTEN24_SHOP_SID = "203677"
FREE_SHIPPING_THRESHOLD = 3980
# 買い時判定を表示するのに必要な履歴日数。これ未満は「データ収集中」と正直に表示する。
MIN_HISTORY_DAYS = 5

FILLER_ITEMS: tuple[dict[str, str], ...] = (
    {"gap_label": "あと200円", "name": "オーエ 不織布キッチンスポンジ", "keyword": "オーエ 不織布 キッチンスポンジ"},
    {"gap_label": "あと300円", "name": "エリエール Plus+キレイ ペーパーハンドタオル", "keyword": "エリエール Plus キレイ ペーパーハンドタオル"},
    {"gap_label": "あと400円", "name": "レック 激落ちくん メラミンスポンジ大容量", "keyword": "レック 激落ちくん メラミンスポンジ"},
    {"gap_label": "あと500円", "name": "クレハ NEWクレラップ（30cm×20m）", "keyword": "クレハ NEWクレラップ 30cm 20m"},
)

# HTMLタブ表示用: category_key → タブフィルタキー
CATEGORY_TAB_MAP: dict[str, str] = {
    "laundry_liquid": "laundry",
    "laundry_powder": "laundry",
    "fabric_softener": "fabric_softener",
    "dish": "dish",
    "bath_toilet": "bath_toilet",
    "body_soap": "body_soap",
}

SITE_CATEGORY_TABS: tuple[tuple[str, str], ...] = (
    ("all", "すべて"),
    ("laundry", "洗濯洗剤"),
    ("fabric_softener", "柔軟剤"),
    ("dish", "食器洗剤"),
    ("bath_toilet", "お風呂・トイレ"),
    ("body_soap", "ハンド・ボディ"),
)


@dataclass(frozen=True)
class TargetProduct:
    """1商品のメタ情報。価格取得は rakuten_item_code (または rakuten_url) を直接 API に渡して行う。

    曖昧なキーワード/JAN検索は使わない。total_shares は必須で、1回あたり単価は
    「取得価格 ÷ total_shares」のみで算出する (容量推定や濃縮倍率推定は一切しない)。
    """

    id: str
    display_name: str
    search_keyword: str
    category_key: str
    jan: str
    total_shares: float
    size_value: float = 0.0
    size_unit: str = ""
    dose_label: str = ""
    rakuten_item_code: str = ""
    rakuten_url: str = ""
    sundrug_item_code: str = ""
    soukai_item_code: str = ""


@dataclass
class ShopOffer:
    shop_key: str
    shop_label: str
    price: Optional[int] = None
    item_name: str = ""
    shop_name: str = ""
    url: str = ""
    price_per_use: Optional[float] = None
    use_unit_label: str = ""
    volume_ml: float = 0.0
    load_count: float = 0.0
    load_count_source: str = ""
    jan_matched: bool = False
    unit_price: Optional[float] = None  # 円/100g または 円/100ml (客観指標)
    unit_basis: str = ""  # "100g" / "100ml"


@dataclass
class TodaySnapshot:
    target: TargetProduct
    offers: dict[str, ShopOffer]
    display_offers: dict[str, ShopOffer] = field(default_factory=dict)

    @property
    def official_offers(self) -> dict[str, ShopOffer]:
        return self.offers

    @property
    def cheapest_offer(self) -> Optional[ShopOffer]:
        """3店舗の公式取得結果を優先。全滅時のみ表示用代替候補へフォールバック。"""
        pools = [
            [o for o in self.offers.values() if o.price_per_use is not None or o.price is not None],
            [
                o
                for o in self.visible_offers.values()
                if not o.shop_label.startswith("代替") and (o.price_per_use is not None or o.price is not None)
            ],
            [o for o in self.visible_offers.values() if o.price_per_use is not None or o.price is not None],
        ]
        for offers in pools:
            if not offers:
                continue
            per_use_offers = [o for o in offers if o.price_per_use is not None]
            if per_use_offers:
                return min(per_use_offers, key=lambda o: o.price_per_use or math.inf)
            return min(
                offers,
                key=lambda o: (
                    o.price_per_use if o.price_per_use is not None else math.inf,
                    o.price if o.price is not None else math.inf,
                ),
            )
        return None

    @property
    def visible_offers(self) -> dict[str, ShopOffer]:
        return self.display_offers or self.offers


@dataclass(frozen=True)
class PriceStats:
    past_min: Optional[float]
    past_avg: Optional[float]
    data_points: int = 0  # 履歴に存在する「日付」の数 (買い時判定の信頼度)
    history: tuple[tuple[str, float], ...] = ()  # (日付, 楽天24価格) の時系列


@dataclass(frozen=True)
class BuySignal:
    label: str
    tone: str
    description: str


@dataclass
class ProductAnalysis:
    snapshot: TodaySnapshot
    stats: PriceStats
    signal: BuySignal
    unit_price: Optional[float] = None       # 楽天24の 円/100g(ml)
    unit_basis: str = ""                      # "100g" / "100ml"
    category_rank: int = 0                    # カテゴリ内の単位価格ランク (1=最安)
    category_size: int = 0                    # カテゴリ内商品数
    is_category_cheapest: bool = False
    cat_min_unit: Optional[float] = None      # カテゴリ内の最安単価 (割安度バー用)
    cat_max_unit: Optional[float] = None      # カテゴリ内の最高単価
    cat_avg_unit: Optional[float] = None       # カテゴリ内の平均単価


def env(name: str, required: bool = False) -> str:
    value = (os.environ.get(name) or "").strip()
    if required and not value:
        raise SystemExit(f"環境変数 {name} が未設定です。.env を確認してください。")
    return value


def ensure_history_csv(path: Path = PRICE_HISTORY_CSV) -> None:
    if path.exists() and path.stat().st_size > 0:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()


def parse_price(value: str | int | float | None) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        price = int(float(text))
    except ValueError:
        return None
    return price if price > 0 else None


def parse_per_use(value: str | int | float | None) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("¥", "")
    if not text:
        return None
    try:
        price = float(text)
    except ValueError:
        return None
    return price if price > 0 else None


def fmt_per_use(value: Optional[float]) -> str:
    return f"¥{value:.1f}" if value is not None else "—"


def per_use_from_total_shares(
    price: int, total_shares: Optional[float]
) -> tuple[Optional[float], float, str]:
    """target_products.json の total_shares から1回あたり単価を算出する。"""
    if not total_shares or total_shares <= 0 or price <= 0:
        return None, 0.0, ""
    return round(price / total_shares, 2), float(total_shares), "total_shares"


def unit_basis_label(size_unit: str) -> str:
    """内容量の単位から単位価格の基準ラベルを返す ('100g' / '100ml')。"""
    return "100g" if (size_unit or "").lower().startswith("g") else "100ml"


def unit_price_per_100(
    price: Optional[int], size_value: float, size_unit: str
) -> tuple[Optional[float], str]:
    """内容量あたりの客観単位価格 (円/100g または 円/100ml) を算出する。

    内容量はメーカー表記の『事実』なので、推定に頼らず比較できる主役指標。
    """
    basis = unit_basis_label(size_unit)
    if price is None or price <= 0 or not size_value or size_value <= 0:
        return None, basis
    return round(price / size_value * 100, 1), basis


def warn_high_per_use(target: TargetProduct, offer: ShopOffer) -> None:
    """1回あたり単価がバイヤー感覚から乖離している場合に警告する。"""
    if offer.price_per_use is None or offer.price_per_use <= PER_USE_WARN_THRESHOLD:
        return
    if target.category_key not in (
        "laundry_liquid",
        "laundry_powder",
        "fabric_softener",
        "dish",
        "bath_toilet",
    ):
        return
    print(
        f"[WARN] 1回あたり単価が高すぎます ({offer.price_per_use:.1f}円): "
        f"{target.display_name} @ {offer.shop_label} "
        f"({offer.item_name or target.display_name}, "
        f"根拠={offer.load_count_source or '不明'})",
        file=sys.stderr,
    )


def shop_product_search_url(target: TargetProduct, shop: ShopConfig) -> str:
    """価格取得失敗時に表示する「店内で探す」リンク。価格取得には使わない。"""
    keyword = target.search_keyword or target.display_name or target.jan
    if shop.key == RAKUTEN24.key:
        return rakuten24_shop_search_url(keyword)
    q = urllib.parse.quote(f"{keyword} {shop.label}")
    return rakuten_affiliate_link(f"https://search.rakuten.co.jp/search/mall/{q}/")


def enrich_offer(offer: ShopOffer, target: TargetProduct, shop: ShopConfig) -> ShopOffer:
    """CSV由来の空欄を total_shares ベースの単価で補完する (容量推定は一切しない)。"""
    category = CATEGORIES.get(target.category_key)
    if not offer.use_unit_label and category:
        offer.use_unit_label = category.use_unit_label
    if not offer.url:
        offer.url = shop_product_search_url(target, shop)
    if offer.price is not None:
        per_use, loads, source = per_use_from_total_shares(offer.price, target.total_shares)
        if per_use is not None:
            offer.price_per_use = per_use
            offer.volume_ml = 0.0
            offer.load_count = loads
            offer.load_count_source = source
        unit, basis = unit_price_per_100(offer.price, target.size_value, target.size_unit)
        offer.unit_price = unit
        offer.unit_basis = basis
    warn_high_per_use(target, offer)
    return offer


def read_history(path: Path = PRICE_HISTORY_CSV) -> list[dict[str, str]]:
    ensure_history_csv(path)
    rows: list[dict[str, str]] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if not row or not row.get("日付") or not row.get("JANコード"):
                    continue
                rows.append({c: str(row.get(c, "") or "") for c in CSV_COLUMNS})
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        print(f"[WARN] {path.name} を読めませんでした。不正データは無視します: {e}", file=sys.stderr)
    return rows


def append_history(snapshots: list[TodaySnapshot], path: Path = PRICE_HISTORY_CSV) -> None:
    ensure_history_csv(path)
    today = date.today().isoformat()
    new_rows = []
    for snapshot in snapshots:
        row = {"日付": today, "JANコード": snapshot.target.jan, "商品名": snapshot.target.display_name}
        per_use_values: list[float] = []
        for shop in SHOPS:
            offer = snapshot.offers.get(shop.key)
            row[shop.csv_column] = str(offer.price) if offer and offer.price is not None else ""
            if offer and offer.price_per_use is not None:
                row[shop.per_use_csv_column] = f"{offer.price_per_use:.1f}"
                per_use_values.append(offer.price_per_use)
            else:
                row[shop.per_use_csv_column] = ""
        row[PER_USE_BEST_COLUMN] = f"{min(per_use_values):.1f}" if per_use_values else ""
        new_rows.append(row)

    existing_rows = read_history(path)
    replace_keys = {(row["日付"], row["JANコード"]) for row in new_rows}
    merged_rows = [
        row
        for row in existing_rows
        if (row.get("日付", ""), row.get("JANコード", "")) not in replace_keys
    ]
    merged_rows.extend(new_rows)

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(merged_rows)


def daily_lowest_per_use(row: dict[str, str]) -> Optional[float]:
    saved = parse_per_use(row.get(PER_USE_BEST_COLUMN))
    if saved is not None:
        return saved
    prices = [parse_per_use(row.get(shop.per_use_csv_column)) for shop in SHOPS]
    prices = [p for p in prices if p is not None]
    return min(prices) if prices else None


def calculate_stats(history_rows: list[dict[str, str]], jan: str) -> PriceStats:
    """楽天24の本体価格履歴を基準に買い時統計を算出する。

    日付ごとに1点へ集約し、価格推移グラフ用の時系列と『信頼できる日数』も返す。
    """
    by_date: dict[str, int] = {}
    for row in history_rows:
        if row.get("JANコード") != jan:
            continue
        price = parse_price(row.get(RAKUTEN24.csv_column))
        if price is not None:
            by_date[row.get("日付", "")] = price  # 同日複数行は最後を採用
    series = tuple(sorted(by_date.items()))
    prices = [p for _, p in series]
    if not prices:
        return PriceStats(None, None, 0, ())
    return PriceStats(min(prices), sum(prices) / len(prices), len(series), series)


def judge_signal(today_price: Optional[int], stats: PriceStats) -> BuySignal:
    """楽天24の本体価格と履歴から買い時を判定する。

    履歴が MIN_HISTORY_DAYS 未満のときは断定せず「データ収集中」と正直に示す。
    """
    if today_price is None:
        return BuySignal("価格取得待ち", "gray", "楽天24の価格を取得できませんでした。後ほど再確認してください。")
    if stats.data_points < MIN_HISTORY_DAYS or stats.past_min is None or stats.past_avg is None:
        return BuySignal(
            "価格データ収集中",
            "blue",
            f"買い時判定には価格履歴が{MIN_HISTORY_DAYS}日分必要です（現在{stats.data_points}日分）。"
            "毎日記録して精度を上げています。",
        )
    if today_price <= stats.past_min:
        return BuySignal("過去最安値", "red", "楽天24の価格が履歴の底値。まとめ買いの好機です。")
    if today_price <= stats.past_avg:
        return BuySignal("買い時（平均以下）", "green", "楽天24の価格が過去平均を下回っています。")
    return BuySignal("やや高め", "slate", "楽天24は過去平均より高め。急ぎでなければセールを待つ手も。")


def extract_item_code_from_url(url: str) -> str:
    """楽天商品URLから ``shopCode:itemNumber`` 形式の itemCode を取り出す。

    対応URL例:
        https://item.rakuten.co.jp/rakuten24/11398575/
        https://item.rakuten.co.jp/rakuten24/11398575
        //item.rakuten.co.jp/rakuten24/11398575/?xxx
    """
    if not url:
        return ""
    m = re.search(r"item\.rakuten\.co\.jp/([^/?#]+)/([^/?#]+)", url)
    if not m:
        return ""
    shop = urllib.parse.unquote(m.group(1)).strip()
    item_number = urllib.parse.unquote(m.group(2)).strip()
    if not shop or not item_number:
        return ""
    return f"{shop}:{item_number}"


def resolve_item_code(target: TargetProduct, shop_key: str) -> str:
    """ターゲットと店舗キーから API に渡す itemCode を解決する。優先順:

    1. shop ごとに明示された ``*_item_code`` フィールド
    2. shop が 'rakuten24' で ``rakuten_url`` が指定されている場合は URL から導出
    """
    if shop_key == "rakuten24":
        if target.rakuten_item_code:
            return target.rakuten_item_code.strip()
        from_url = extract_item_code_from_url(target.rakuten_url)
        if from_url:
            return from_url
        return ""
    return (getattr(target, f"{shop_key}_item_code", "") or "").strip()


def load_targets(path: Path = TARGET_PRODUCTS_PATH) -> list[TargetProduct]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = raw.get("products") if isinstance(raw, dict) else raw
    targets: list[TargetProduct] = []
    skipped: list[str] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        jan = str(entry.get("jan", "") or "").strip()
        if not jan:
            continue
        raw_shares = entry.get("total_shares")
        try:
            total_shares = float(raw_shares) if raw_shares not in (None, "", 0) else 0.0
        except (TypeError, ValueError):
            total_shares = 0.0
        if total_shares <= 0:
            skipped.append(f"{entry.get('id') or jan}: total_shares 未設定/無効")
            continue
        try:
            size_value = float(entry.get("size_value") or 0)
        except (TypeError, ValueError):
            size_value = 0.0
        size_unit = str(entry.get("size_unit") or "").strip().lower()
        targets.append(
            TargetProduct(
                id=str(entry.get("id") or jan),
                display_name=str(entry.get("display_name") or entry.get("search_keyword") or jan),
                search_keyword=str(entry.get("search_keyword") or entry.get("display_name") or jan),
                category_key=str(entry.get("category_key") or "laundry_liquid"),
                jan=jan,
                total_shares=total_shares,
                size_value=size_value,
                size_unit=size_unit,
                dose_label=str(entry.get("dose_label") or "").strip(),
                rakuten_item_code=str(entry.get("rakuten_item_code") or "").strip(),
                rakuten_url=str(entry.get("rakuten_url") or "").strip(),
                sundrug_item_code=str(entry.get("sundrug_item_code") or "").strip(),
                soukai_item_code=str(entry.get("soukai_item_code") or "").strip(),
            )
        )
    if skipped:
        for msg in skipped:
            print(f"[WARN] target_products.json スキップ: {msg}", file=sys.stderr)
    if not targets:
        raise SystemExit("target_products.json に有効な商品がありません (total_shares 必須)。")
    return targets


class RakutenClient:
    def __init__(self) -> None:
        self.app_id = env("RAKUTEN_APP_ID", required=True)
        self.access_key = env("RAKUTEN_ACCESS_KEY", required=True)
        self.affiliate_id = env("RAKUTEN_AFFILIATE_ID")
        self.referer = env("RAKUTEN_REFERER")
        self.last_request_at = 0.0

    def headers(self) -> dict[str, str]:
        if not self.referer:
            return {}
        ref = self.referer if self.referer.startswith(("http://", "https://")) else "https://" + self.referer.lstrip("/")
        # Origin はパスを含めず scheme://host のみが正しい (楽天APIの照合もこの単位)。
        parsed = urllib.parse.urlparse(ref)
        origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else ref
        return {"Referer": ref, "Origin": origin}

    def search(
        self,
        keyword: str = "",
        hits: int = 30,
        shop_code: str = "",
        item_code: str = "",
    ) -> list[dict]:
        """楽天 IchibaItem/Search 呼び出し。

        item_code を渡した場合は ``itemCode`` パラメタでピンポイントに1商品だけ取得する。
        その場合 keyword / sort / availability などは付けず、API のレスポンスを
        そのまま返す (該当商品が在庫切れなら 0 件)。
        """
        if not keyword and not item_code:
            raise ValueError("keyword か item_code のどちらかが必須です")
        elapsed = time.time() - self.last_request_at
        if 0 < elapsed < 1.5:
            time.sleep(1.5 - elapsed)
        params: dict[str, str | int] = {
            "applicationId": self.app_id,
            "accessKey": self.access_key,
            "hits": max(1, min(hits, 30)),
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
        if self.affiliate_id:
            params["affiliateId"] = self.affiliate_id
        response = requests.get(RAKUTEN_API_URL, params=params, headers=self.headers(), timeout=12)
        self.last_request_at = time.time()
        response.raise_for_status()
        payload = response.json()
        return payload.get("items") or payload.get("Items") or []


def unwrap_item(entry: dict) -> dict:
    """formatVersion=2 では既にフラットだが、稀に旧形式 {"Item": {...}} が混ざるので展開する。"""
    return entry.get("Item", entry) if isinstance(entry, dict) else {}


def fetch_offer_by_item_code(
    client: RakutenClient,
    item_code: str,
    target: TargetProduct,
    shop: ShopConfig,
) -> Optional[ShopOffer]:
    """itemCode を直接 API に渡し、ピンポイントで現在価格と URL を取得する。

    返り値:
        在庫切れ / 取扱終了で API が 0 件を返した場合は ``None``。
        その場合は呼び出し側で「店内検索ボタンのみ」のプレースホルダ ShopOffer を作る。
    """
    items = client.search(item_code=item_code, hits=1)
    if not items:
        return None
    item = unwrap_item(items[0])
    price = parse_price(item.get("itemPrice"))
    if price is None:
        return None
    category = CATEGORIES.get(target.category_key)
    per_use, loads, source = per_use_from_total_shares(price, target.total_shares)
    unit, basis = unit_price_per_100(price, target.size_value, target.size_unit)
    actual_shop = str(item.get("shopName") or shop.label)
    actual_shop_code = str(item.get("shopCode") or "").lower()
    expected_shop_codes = {c.lower() for c in shop.shop_codes}
    is_expected_shop = (
        not expected_shop_codes
        or not actual_shop_code
        or actual_shop_code in expected_shop_codes
    )
    shop_label = shop.label if is_expected_shop else f"代替: {actual_shop}"
    offer = ShopOffer(
        shop_key=shop.key,
        shop_label=shop_label,
        price=price,
        item_name=str(item.get("itemName") or target.display_name),
        shop_name=actual_shop,
        url=str(item.get("affiliateUrl") or item.get("itemUrl") or ""),
        price_per_use=per_use,
        use_unit_label=category.use_unit_label if category else "",
        volume_ml=0.0,
        load_count=loads,
        load_count_source=source or "total_shares",
        jan_matched=True,
        unit_price=unit,
        unit_basis=basis,
    )
    warn_high_per_use(target, offer)
    return offer


def rakuten_search_url(target: TargetProduct) -> str:
    """itemCode が未設定の店舗向けプレースホルダ用の楽天市場検索 URL。"""
    q = urllib.parse.quote(target.search_keyword or target.display_name or target.jan)
    return f"https://search.rakuten.co.jp/search/mall/{q}/"


def collect_snapshots(targets: list[TargetProduct]) -> list[TodaySnapshot]:
    """各ターゲットの3店舗価格を itemCode 指定でピンポイント取得する。

    曖昧なキーワード/JAN検索は一切しない。itemCode が未設定の店舗スロットは
    プレースホルダ (店内検索リンクのみ) を返す。
    """
    client = RakutenClient()
    snapshots: list[TodaySnapshot] = []
    for i, target in enumerate(targets, 1):
        print(f"[{i}/{len(targets)}] 価格取得: {target.display_name}", file=sys.stderr)
        offers: dict[str, ShopOffer] = {}
        for shop in SHOPS:
            item_code = resolve_item_code(target, shop.key)
            offer: Optional[ShopOffer] = None
            if item_code:
                try:
                    offer = fetch_offer_by_item_code(client, item_code, target, shop)
                except requests.HTTPError as e:
                    status = e.response.status_code if e.response is not None else "?"
                    body = (e.response.text[:120] if e.response is not None else "")
                    print(
                        f"[WARN] {target.display_name} {shop.label} "
                        f"itemCode={item_code}: HTTP {status} {body}",
                        file=sys.stderr,
                    )
                except requests.RequestException as e:
                    print(
                        f"[WARN] {target.display_name} {shop.label} "
                        f"itemCode={item_code}: {e}",
                        file=sys.stderr,
                    )
            if offer is None:
                offer = ShopOffer(
                    shop_key=shop.key,
                    shop_label=shop.label,
                    url=shop_product_search_url(target, shop),
                )
            offers[shop.key] = offer
        snapshots.append(TodaySnapshot(target, offers, dict(offers)))
    return snapshots


def assign_category_ranks(results: list[ProductAnalysis]) -> None:
    """同一カテゴリ内で単位価格 (円/100g・ml) の安い順にランクを振る。"""
    by_cat: dict[str, list[ProductAnalysis]] = {}
    for a in results:
        by_cat.setdefault(a.snapshot.target.category_key, []).append(a)
    for items in by_cat.values():
        ranked = [a for a in items if a.unit_price is not None]
        ranked.sort(key=lambda a: a.unit_price or math.inf)
        size = len(ranked)
        prices = [a.unit_price for a in ranked if a.unit_price is not None]
        cmin = min(prices) if prices else None
        cmax = max(prices) if prices else None
        cavg = (sum(prices) / len(prices)) if prices else None
        for i, a in enumerate(ranked, 1):
            a.category_rank = i
            a.category_size = size
            a.is_category_cheapest = i == 1
        for a in items:
            a.category_size = size
            a.cat_min_unit = cmin
            a.cat_max_unit = cmax
            a.cat_avg_unit = cavg


def analyze(snapshots: list[TodaySnapshot], history_rows: list[dict[str, str]]) -> list[ProductAnalysis]:
    results = []
    for snapshot in snapshots:
        stats = calculate_stats(history_rows, snapshot.target.jan)
        r24 = snapshot.visible_offers.get(RAKUTEN24.key) or snapshot.offers.get(RAKUTEN24.key)
        today_price = r24.price if r24 else None
        results.append(
            ProductAnalysis(
                snapshot,
                stats,
                judge_signal(today_price, stats),
                unit_price=r24.unit_price if r24 else None,
                unit_basis=(r24.unit_basis if r24 else "") or unit_basis_label(snapshot.target.size_unit),
            )
        )
    assign_category_ranks(results)
    return results


def amazon_url(target: TargetProduct) -> str:
    q = urllib.parse.quote(target.jan or target.display_name)
    url = f"https://www.amazon.co.jp/s?k={q}"
    if env("AMAZON_ASSOCIATE_TAG"):
        url += f"&tag={urllib.parse.quote(env('AMAZON_ASSOCIATE_TAG'))}"
    return url


def fmt_price(price: Optional[int]) -> str:
    return f"¥{price:,}" if price is not None else "—"


def offer_detail(offer: ShopOffer) -> str:
    parts = []
    if offer.price is not None:
        parts.append(f"本体 {fmt_price(offer.price)}")
    if offer.load_count:
        parts.append(f"{offer.load_count:.1f}回分")
    if offer.volume_ml:
        parts.append(f"{offer.volume_ml:.0f}ml")
    if offer.load_count_source:
        parts.append(f"推定:{offer.load_count_source}")
    if offer.jan_matched:
        parts.append("JAN一致")
    if offer.price is not None and offer.price_per_use is None:
        parts.append("容量読取不可")
    return " / ".join(parts) if parts else "リンク先で価格確認"


def fmt_avg(price: Optional[float]) -> str:
    return f"¥{price:,.0f}" if price is not None else "—"


def signal_class(tone: str) -> str:
    return {
        "red": "bg-red-100 text-red-700 border-red-200",
        "green": "bg-emerald-100 text-emerald-700 border-emerald-200",
        "slate": "bg-slate-100 text-slate-700 border-slate-200",
        "blue": "bg-sky-100 text-sky-700 border-sky-200",
        "gray": "bg-gray-100 text-gray-600 border-gray-200",
    }.get(tone, "bg-gray-100 text-gray-600 border-gray-200")


def rakuten_affiliate_link(target_url: str) -> str:
    affiliate_id = env("RAKUTEN_AFFILIATE_ID")
    if not affiliate_id:
        return target_url
    pc = urllib.parse.quote(target_url, safe="")
    m_url = target_url.replace("https://", "http://m.")
    m = urllib.parse.quote(m_url, safe="")
    return f"https://hb.afl.rakuten.co.jp/hgc/{affiliate_id}/?pc={pc}&m={m}"


def category_tab_key(category_key: str) -> str:
    return CATEGORY_TAB_MAP.get(category_key, category_key)


def render_category_tabs() -> str:
    buttons: list[str] = []
    for tab_key, label in SITE_CATEGORY_TABS:
        active = " category-tab-active" if tab_key == "all" else ""
        buttons.append(
            f'<button type="button" data-tab="{html.escape(tab_key)}" '
            f'class="category-tab shrink-0 px-4 py-2.5 rounded-full text-sm font-bold border transition-all duration-200{active}">'
            f"{html.escape(label)}</button>"
        )
    return f"""
<div class="sticky top-0 z-30 bg-slate-50/85 backdrop-blur-md border-b border-slate-200/70">
<section class="max-w-6xl mx-auto px-4 sm:px-6 py-3">
  <div class="flex gap-2 overflow-x-auto -mx-1 px-1 scrollbar-hide" id="category-tabs" role="tablist">
    {''.join(buttons)}
  </div>
  <p class="text-[11px] text-gray-400 mt-1.5 px-1" id="category-tab-count"></p>
</section>
</div>
<style>
  .scrollbar-hide::-webkit-scrollbar {{ display: none; }}
  .scrollbar-hide {{ -ms-overflow-style: none; scrollbar-width: none; }}
  .category-tab {{ background: #fff; color: #64748b; border-color: #e2e8f0; }}
  .category-tab:hover {{ border-color: #fca5a5; color: #dc2626; }}
  .category-tab-active {{ background: #dc2626 !important; color: #fff !important; border-color: #dc2626 !important; box-shadow: 0 4px 14px rgba(220,38,38,.25); }}
  .product-card {{ transition: opacity .2s ease, transform .2s ease; }}
  .product-card.is-hidden {{ display: none !important; }}
</style>
""".strip()


CATEGORY_FILTER_SCRIPT = """
<script>
(function() {
  function initCategoryTabs() {
    var tabs = document.querySelectorAll('.category-tab');
    var countEl = document.getElementById('category-tab-count');
    function applyTab(tab) {
      var cards = document.querySelectorAll('.product-card');
      var visible = 0;
      cards.forEach(function(card) {
        var cat = card.getAttribute('data-category') || '';
        var show = tab === 'all' || cat === tab;
        card.classList.toggle('is-hidden', !show);
        if (show) visible++;
      });
      tabs.forEach(function(btn) {
        btn.classList.toggle('category-tab-active', btn.getAttribute('data-tab') === tab);
      });
      if (countEl) countEl.textContent = visible + ' 件表示中';
    }
    tabs.forEach(function(btn) {
      btn.addEventListener('click', function() { applyTab(btn.getAttribute('data-tab')); });
    });
    applyTab('all');
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initCategoryTabs);
  } else {
    initCategoryTabs();
  }
})();
</script>
""".strip()


def rakuten24_shop_search_url(keyword: str) -> str:
    q = urllib.parse.quote(keyword)
    base = f"https://search.rakuten.co.jp/search/mall/{q}/?sid={RAKUTEN24_SHOP_SID}"
    return rakuten_affiliate_link(base)


CATEGORY_UI: dict[str, dict[str, str]] = {
    "laundry_liquid": {"emoji": "🧺", "accent": "sky", "label": "液体洗濯洗剤"},
    "laundry_powder": {"emoji": "📦", "accent": "indigo", "label": "粉末洗濯洗剤"},
    "fabric_softener": {"emoji": "🌸", "accent": "violet", "label": "柔軟剤"},
    "dish": {"emoji": "🍽️", "accent": "emerald", "label": "食器用洗剤"},
    "bath_toilet": {"emoji": "🛁", "accent": "cyan", "label": "浴室・トイレ"},
    "body_soap": {"emoji": "🧼", "accent": "rose", "label": "ボディソープ"},
}


def category_ui(category_key: str) -> dict[str, str]:
    ui = CATEGORY_UI.get(category_key)
    if ui:
        return ui
    cat = CATEGORIES.get(category_key)
    return {"emoji": "🧴", "accent": "slate", "label": cat.display_name if cat else category_key}


def fmt_unit_price(value: Optional[float], basis: str) -> str:
    """単位価格を '¥12.3 /100g' 形式で整形する。"""
    if value is None:
        return "—"
    return f"¥{value:,.1f} <span class='text-base font-bold text-gray-400'>/{html.escape(basis or '100g')}</span>"


def render_sparkline(history: tuple[tuple[str, int], ...], width: int = 220, height: int = 48) -> str:
    """楽天24本体価格の推移を簡易SVGスパークラインで描く。点が2未満なら案内文。"""
    points = [p for _, p in history if p is not None]
    if len(points) < 2:
        return (
            '<div class="flex items-center gap-2 text-[11px] text-sky-600 bg-sky-50 '
            'rounded-lg px-3 py-2"><span>📈</span>'
            f'<span>価格推移は記録{len(points)}日分。毎日更新でグラフが育ちます。</span></div>'
        )
    lo, hi = min(points), max(points)
    span = (hi - lo) or 1
    n = len(points)
    pad = 4
    step = (width - pad * 2) / (n - 1)
    coords = []
    for i, p in enumerate(points):
        x = pad + step * i
        y = pad + (height - pad * 2) * (1 - (p - lo) / span)
        coords.append((x, y))
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    last_x, last_y = coords[-1]
    area = f"{pad},{height - pad} " + line + f" {last_x:.1f},{height - pad}"
    trend_down = points[-1] <= points[0]
    stroke = "#16a34a" if trend_down else "#dc2626"
    fill = "rgba(22,163,74,.08)" if trend_down else "rgba(220,38,38,.08)"
    delta = points[-1] - points[0]
    if delta < 0:
        note = f'<span class="text-emerald-600 font-bold">▼ ¥{abs(delta):,}</span>'
    elif delta > 0:
        note = f'<span class="text-red-600 font-bold">▲ ¥{delta:,}</span>'
    else:
        note = '<span class="text-gray-400">変動なし</span>'
    return (
        '<div>'
        '<div class="flex items-center justify-between text-[10px] text-gray-400 mb-0.5">'
        f'<span>価格推移（楽天24・{len(points)}日）</span>{note}</div>'
        f'<svg viewBox="0 0 {width} {height}" class="w-full h-10" preserveAspectRatio="none" '
        f'role="img" aria-label="価格推移">'
        f'<polygon points="{area}" fill="{fill}" />'
        f'<polyline points="{line}" fill="none" stroke="{stroke}" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round" />'
        f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="3" fill="{stroke}" />'
        f"</svg></div>"
    )


def render_value_bar(analysis: ProductAnalysis) -> str:
    """カテゴリ内の単価レンジに対する『割安度』を水平バーで可視化する。"""
    u = analysis.unit_price
    lo, hi, avg = analysis.cat_min_unit, analysis.cat_max_unit, analysis.cat_avg_unit
    if u is None or lo is None or hi is None or analysis.category_size < 2:
        return ""
    span = (hi - lo) or 1
    pos = max(0.0, min(1.0, (u - lo) / span)) * 100  # 0%=最安, 100%=最高
    avg_pos = max(0.0, min(1.0, ((avg or u) - lo) / span)) * 100
    # 平均比 (安いほどプラス表現)
    diff_txt = ""
    if avg and avg > 0:
        pct = (avg - u) / avg * 100
        if pct >= 1:
            diff_txt = f'<span class="text-emerald-600 font-bold">平均より{pct:.0f}%安い</span>'
        elif pct <= -1:
            diff_txt = f'<span class="text-slate-500 font-bold">平均より{abs(pct):.0f}%高い</span>'
        else:
            diff_txt = '<span class="text-slate-500 font-bold">ほぼ平均</span>'
    return (
        '<div class="mt-3">'
        '<div class="flex items-center justify-between text-[10px] text-gray-400 mb-1">'
        '<span>カテゴリ内 安い</span>'
        f'<span>{diff_txt}</span>'
        '<span>高い</span></div>'
        '<div class="relative h-2 rounded-full bg-gradient-to-r from-emerald-400 via-amber-300 to-rose-400">'
        f'<span class="absolute top-1/2 -translate-y-1/2 -translate-x-1/2 w-1 h-3.5 rounded-full bg-slate-700/40" style="left:{avg_pos:.0f}%"></span>'
        f'<span class="absolute top-1/2 -translate-y-1/2 -translate-x-1/2 w-4 h-4 rounded-full bg-white border-2 border-gray-800 shadow" style="left:{pos:.0f}%"></span>'
        '</div></div>'
    )


def rank_badge(analysis: ProductAnalysis) -> str:
    """カテゴリ内の単位価格ランクをバッジ表示する。"""
    if not analysis.category_rank or not analysis.category_size:
        return ""
    cat = CATEGORIES.get(analysis.snapshot.target.category_key)
    cat_name = cat.display_name if cat else "同種"
    if analysis.is_category_cheapest:
        return (
            '<span class="inline-flex items-center gap-1 text-xs font-black px-3 py-1 rounded-full '
            f'bg-amber-400 text-amber-900 shadow-sm">🥇 {html.escape(cat_name)}で単価最安</span>'
        )
    return (
        '<span class="inline-flex items-center gap-1 text-xs font-bold px-3 py-1 rounded-full '
        f'bg-slate-100 text-slate-600 border border-slate-200">{html.escape(cat_name)} '
        f'{analysis.category_rank}位 / {analysis.category_size}品</span>'
    )


def button(label: str, url: str, classes: str, disabled: bool = False) -> str:
    if disabled or not url:
        return f'<span class="text-center text-xs px-3 py-2 rounded-lg bg-gray-200 text-gray-400">{html.escape(label)}</span>'
    return (
        f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="nofollow sponsored noopener" '
        f'class="text-center text-xs px-3 py-2 rounded-lg font-bold text-white shadow-sm hover:opacity-90 {classes}">'
        f'{html.escape(label)}</a>'
    )


def render_method_section() -> str:
    """価値提案と算出根拠の透明化セクション (信頼性の核)。"""
    return """
<section class="max-w-6xl mx-auto px-4 sm:px-6 pt-6 pb-2">
  <div class="rounded-3xl bg-white border border-gray-200 shadow-sm p-6 sm:p-8">
    <p class="text-xs font-black tracking-widest uppercase text-red-600">How it works / 比較のしくみ</p>
    <h2 class="text-xl sm:text-2xl font-black mt-2 text-gray-900 leading-snug">「内容量あたりの単価」で、同じ種類の中で本当に割安な1本を選ぶ。</h2>
    <p class="text-sm text-gray-600 mt-2 leading-relaxed">本体価格やセール表示に惑わされず、<strong>価格 ÷ 内容量</strong>で計算した客観的な単位価格（円/100g・円/100ml）で横並び比較。詰め替え・大容量も公平に見比べられます。</p>
    <div class="mt-5 grid gap-3 sm:grid-cols-3">
      <div class="rounded-2xl bg-slate-50 border border-slate-100 p-4">
        <p class="text-2xl mb-1">⚖️</p>
        <p class="text-sm font-bold text-gray-900">単位価格（主指標）</p>
        <p class="text-xs text-gray-600 mt-1 leading-relaxed">価格÷内容量×100。メーカー表記の容量から計算する<strong>事実ベース</strong>の数値です。</p>
      </div>
      <div class="rounded-2xl bg-slate-50 border border-slate-100 p-4">
        <p class="text-2xl mb-1">🧴</p>
        <p class="text-sm font-bold text-gray-900">1回あたり（目安）</p>
        <p class="text-xs text-gray-600 mt-1 leading-relaxed">標準使用量で割った参考値。使用量は商品で差があるため<strong>目安</strong>として表示します。</p>
      </div>
      <div class="rounded-2xl bg-slate-50 border border-slate-100 p-4">
        <p class="text-2xl mb-1">📈</p>
        <p class="text-sm font-bold text-gray-900">価格推移と買い時</p>
        <p class="text-xs text-gray-600 mt-1 leading-relaxed">日々の価格を記録。十分な履歴が貯まったら過去最安・平均と比べて買い時を示します。</p>
      </div>
    </div>
    <p class="text-xs text-gray-400 mt-4 leading-relaxed">価格データ元: 楽天24（楽天市場）の商品APIから取得した本体価格。送料無料ライン ¥3,980〜。価格は変動するため、購入前に必ずリンク先の表示価格・在庫をご確認ください。</p>
  </div>
</section>
""".strip()


def render_best_buys_section(analyses: list[ProductAnalysis]) -> str:
    """各カテゴリの単位価格最安を『今日のベストバイ』として並べる。"""
    cards: list[str] = []
    for cat_key in ("laundry_liquid", "laundry_powder", "fabric_softener", "dish", "bath_toilet", "body_soap"):
        cands = [
            a for a in analyses
            if a.snapshot.target.category_key == cat_key and a.unit_price is not None
        ]
        if not cands:
            continue
        best = min(cands, key=lambda a: a.unit_price or math.inf)
        ui = category_ui(cat_key)
        accent = ui["accent"]
        r24 = best.snapshot.visible_offers.get(RAKUTEN24.key) or ShopOffer(RAKUTEN24.key, RAKUTEN24.label)
        name = best.snapshot.target.display_name
        savings = ""
        if best.cat_avg_unit and best.unit_price and best.cat_avg_unit > best.unit_price:
            pct = (best.cat_avg_unit - best.unit_price) / best.cat_avg_unit * 100
            if pct >= 1:
                savings = f'<span class="text-[10px] font-bold text-emerald-600 bg-emerald-50 px-1.5 py-0.5 rounded">平均比 -{pct:.0f}%</span>'
        cards.append(
            '<a href="' + html.escape(r24.url or "#", quote=True) + '" target="_blank" '
            'rel="nofollow sponsored noopener" '
            'class="group block rounded-2xl bg-white border border-gray-100 shadow-sm hover:shadow-lg '
            'hover:-translate-y-1 transition-all duration-200 overflow-hidden">'
            f'<div class="h-1.5 bg-{accent}-400"></div>'
            '<div class="p-4">'
            '<div class="flex items-center justify-between">'
            f'<p class="text-[11px] font-bold text-{accent}-700 bg-{accent}-50 px-2 py-0.5 rounded-full inline-flex items-center gap-1">{ui["emoji"]} {html.escape(ui["label"])}</p>'
            f'<span class="text-[10px] font-black text-amber-600">🥇 最安</span>'
            '</div>'
            f'<p class="text-sm font-bold text-gray-900 mt-2 leading-snug line-clamp-2 min-h-[2.5rem]">{html.escape(name)}</p>'
            f'<p class="text-2xl font-black text-gray-900 tabular-nums mt-1">{fmt_unit_price(best.unit_price, best.unit_basis)}</p>'
            f'<div class="flex items-center gap-2 mt-1.5 flex-wrap"><span class="text-[11px] text-gray-500">本体 {fmt_price(r24.price)}</span>{savings}</div>'
            '</div></a>'
        )
    if not cards:
        return ""
    return (
        '<section class="max-w-6xl mx-auto px-4 sm:px-6 py-8">'
        '<div class="mb-4">'
        '<h2 class="text-lg sm:text-xl font-black text-gray-900">🏆 今日のベストバイ</h2>'
        '<p class="text-xs text-gray-500 mt-0.5">カテゴリごとに「内容量あたりの単価」が一番安い1本</p>'
        '</div>'
        f'<div class="grid gap-3 grid-cols-2 lg:grid-cols-3">{"".join(cards)}</div>'
        '</section>'
    )


def render_filler_section() -> str:
    cards: list[str] = []
    for item in FILLER_ITEMS:
        url = rakuten24_shop_search_url(item["keyword"])
        cards.append(
            '<div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 rounded-2xl bg-white border border-amber-100 p-4 shadow-sm hover:shadow-md transition-shadow">'
            "<div>"
            f'<span class="inline-block text-[11px] font-black px-2 py-0.5 rounded-full bg-amber-100 text-amber-800 mb-1">{html.escape(item["gap_label"])}</span>'
            f'<p class="text-sm font-bold text-gray-900">{html.escape(item["name"])}</p>'
            '<p class="text-[11px] text-gray-500 mt-0.5">390円均一ゾーン｜腐らない調整用お宝</p>'
            "</div>"
            + button("楽天24で探す", url, "bg-red-600 shrink-0 px-4 py-2 text-xs")
            + "</div>"
        )
    return (
        '<section class="max-w-6xl mx-auto px-4 sm:px-6 py-10">'
        '<div class="rounded-3xl bg-gradient-to-b from-amber-50 to-orange-50 border border-amber-200 p-6 sm:p-8">'
        '<div class="flex items-start gap-3 mb-6">'
        '<span class="text-3xl shrink-0">🛒</span>'
        '<div>'
        '<h2 class="text-xl font-black text-gray-900">あと一息で送料無料！390円均一・調整用お宝リスト</h2>'
        '<p class="text-sm text-gray-600 mt-1">3,980円ラインにあと数百円足りない時、カゴに放り込めるバイヤー厳選の消耗品。すべて楽天24店内検索です。</p>'
        '</div></div>'
        f'<div class="grid gap-3 sm:grid-cols-2">{"".join(cards)}</div>'
        '</div></section>'
    )


def render_card(analysis: ProductAnalysis) -> str:
    snapshot = analysis.snapshot
    target = snapshot.target
    r24 = snapshot.visible_offers.get(RAKUTEN24.key) or ShopOffer(RAKUTEN24.key, RAKUTEN24.label)
    cat = CATEGORIES.get(target.category_key)
    unit = r24.use_unit_label or (cat.use_unit_label if cat else "1回")
    ui = category_ui(target.category_key)
    accent = ui["accent"]

    size_txt = ""
    if target.size_value:
        u = "g" if (target.size_unit or "").startswith("g") else "ml"
        size_txt = f"{target.size_value:g}{u}"

    per_use_line = ""
    if r24.price_per_use is not None:
        dose = f"（{html.escape(target.dose_label)}）" if target.dose_label else ""
        per_use_line = (
            f'<div class="rounded-lg bg-slate-50 border border-slate-100 px-3 py-2">'
            f'<span class="text-gray-500">1回あたり目安</span> '
            f'<b class="text-gray-800">{fmt_per_use(r24.price_per_use)}</b>'
            f'<span class="text-[10px] text-gray-400"> /{html.escape(unit)}{dose}</span></div>'
        )

    no_price = r24.price is None
    if analysis.stats.data_points >= MIN_HISTORY_DAYS and analysis.stats.past_min is not None:
        hist_line = (
            '<div class="grid grid-cols-2 gap-2 mt-3 text-xs">'
            f'<div class="rounded-lg bg-white border border-gray-100 px-3 py-2">過去最安 '
            f'<b class="text-red-600">{fmt_price(int(analysis.stats.past_min))}</b></div>'
            f'<div class="rounded-lg bg-white border border-gray-100 px-3 py-2">平均 '
            f'<b class="text-gray-700">{fmt_price(int(analysis.stats.past_avg))}</b></div>'
            '</div>'
        )
    else:
        hist_line = ""

    cta = button("楽天24で見る →", r24.url, "bg-red-600 w-full text-sm py-3", not r24.url)
    amazon = button("Amazonで価格を見る", amazon_url(target), "bg-slate-700 w-full text-xs py-2")
    top_ring = "ring-2 ring-amber-300" if analysis.is_category_cheapest else "border border-gray-100"

    card = f"""
<article class="product-card bg-white rounded-2xl shadow-sm {top_ring} overflow-hidden flex flex-col hover:shadow-lg hover:-translate-y-0.5 transition-all duration-200" data-category="{html.escape(category_tab_key(target.category_key))}">
  <div class="h-1.5 bg-{accent}-400"></div>
  <header class="px-5 pt-4 pb-3">
    <div class="flex items-center justify-between gap-2 mb-2">
      <span class="inline-flex items-center gap-1 text-[11px] font-bold text-{accent}-700 bg-{accent}-50 px-2 py-1 rounded-full">{ui['emoji']} {html.escape(ui['label'])}</span>
      <span class="shrink-0 text-[11px] font-bold px-2.5 py-1 rounded-full border {signal_class(analysis.signal.tone)}">{html.escape(analysis.signal.label)}</span>
    </div>
    <h2 class="text-base font-extrabold text-gray-900 leading-snug">{html.escape(target.display_name)}</h2>
    <div class="mt-2">{rank_badge(analysis)}</div>
  </header>
  <div class="px-5 pb-5 flex-1 flex flex-col">
    <div class="rounded-xl bg-gradient-to-br from-slate-50 to-white border border-slate-100 p-4">
      <p class="text-[11px] text-gray-500 font-medium">内容量あたりの単価（客観指標）</p>
      <p class="text-3xl font-black text-gray-900 tabular-nums mt-0.5">{fmt_unit_price(analysis.unit_price, analysis.unit_basis)}</p>
      {render_value_bar(analysis)}
    </div>
    {'<p class="text-xs text-amber-700 mt-2">※ 現在価格を取得できませんでした</p>' if no_price else ''}
    <div class="grid grid-cols-1 gap-2 mt-3 text-xs">
      <div class="rounded-lg bg-slate-50 border border-slate-100 px-3 py-2 flex justify-between"><span class="text-gray-500">本体価格{(' ・ ' + html.escape(size_txt)) if size_txt else ''}</span> <b class="text-gray-800">{fmt_price(r24.price)}</b></div>
      {per_use_line}
    </div>
    {hist_line}
    <div class="mt-3">{render_sparkline(analysis.stats.history)}</div>
    <p class="text-xs text-gray-600 mt-3 leading-relaxed flex-1">{html.escape(analysis.signal.description)}</p>
    <div class="mt-4 space-y-2">
      {cta}
      {amazon}
    </div>
  </div>
</article>
""".strip()
    return card


def generate_html(analyses: list[ProductAnalysis], out: Path = DEFAULT_HTML_PATH) -> None:
    # g と ml は直接比較できないため、カテゴリ順→カテゴリ内で単価の安い順に並べる
    # (各カードのランクバッジと並び順を一致させ、誤解を避ける)。
    cat_order = {key: i for i, (key, _) in enumerate(CATEGORIES.items())}
    ordered = sorted(
        analyses,
        key=lambda a: (
            cat_order.get(a.snapshot.target.category_key, 99),
            a.unit_price is None,
            a.unit_price if a.unit_price is not None else math.inf,
        ),
    )
    cards = "\n".join(render_card(a) for a in ordered)
    updated = datetime.now().strftime("%Y-%m-%d %H:%M")
    method = render_method_section()
    best = render_best_buys_section(analyses)
    tabs = render_category_tabs()
    filler = render_filler_section()
    priced = sum(1 for a in analyses if a.unit_price is not None)
    out.write_text(
        f"""<!DOCTYPE html>
<html lang="ja"><head>
<meta charset="utf-8" /><meta name="viewport" content="width=device-width,initial-scale=1" />
<title>洗剤・日用品の単価比較 | 内容量あたりで本当に安い1本を探す</title>
<meta name="description" content="洗剤・柔軟剤・食器用洗剤などを内容量あたりの単価（円/100g・円/100ml）で客観比較。楽天24の価格を毎日記録し、価格推移と買い時もチェックできます。" />
<script src="https://cdn.tailwindcss.com"></script>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;700;900&family=Noto+Sans+JP:wght@400;700;900&display=swap" rel="stylesheet">
<style>html{{font-family:'Inter','Noto Sans JP',system-ui,sans-serif}}.tabular-nums{{font-variant-numeric:tabular-nums}}.line-clamp-2{{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}</style>
</head><body class="bg-gradient-to-b from-slate-50 to-slate-100 min-h-screen text-gray-900">
<header class="relative overflow-hidden bg-gradient-to-br from-rose-600 via-red-500 to-orange-500 text-white">
  <div class="absolute -top-16 -right-16 w-64 h-64 rounded-full bg-white/10 blur-2xl"></div>
  <div class="absolute -bottom-24 -left-10 w-72 h-72 rounded-full bg-amber-300/20 blur-3xl"></div>
  <div class="relative max-w-6xl mx-auto px-4 sm:px-6 py-9 sm:py-12">
    <p class="text-[11px] sm:text-xs font-black tracking-[0.2em] uppercase text-white/80">単位価格で選ぶ 洗剤・日用品比較</p>
    <h1 class="text-2xl sm:text-4xl font-black mt-2 leading-tight">内容量あたりの単価で、<br class="sm:hidden">本当に割安な<span class="underline decoration-amber-300 decoration-4 underline-offset-4">1本</span>を見つける。</h1>
    <p class="text-sm sm:text-base text-white/90 mt-3 max-w-2xl leading-relaxed">洗剤・柔軟剤・食器用洗剤などを「円/100g・円/100ml」で客観比較。詰め替えや大容量も公平に見比べられます。</p>
    <div class="flex flex-wrap items-center gap-2 mt-5 text-[11px] sm:text-xs">
      <span class="px-3 py-1.5 rounded-full bg-white/20 backdrop-blur-sm font-bold">📊 {len(analyses)}商品を比較</span>
      <span class="px-3 py-1.5 rounded-full bg-white/20 backdrop-blur-sm font-bold">✅ 価格取得済み {priced}件</span>
      <span class="px-3 py-1.5 rounded-full bg-white/15 backdrop-blur-sm">🕒 最終更新 {html.escape(updated)}</span>
    </div>
  </div>
</header>
{method}
{best}
{tabs}
<main class="max-w-6xl mx-auto px-4 sm:px-6 py-4">
<h2 class="text-lg font-black text-gray-800 mb-1">全商品一覧（カテゴリ別・単価の安い順）</h2>
<p class="text-xs text-gray-500 mb-4">同じカテゴリ内で単価が安い順に並んでいます。上のタブで絞り込めます。金額は楽天24の取得価格に基づきます。</p>
<div class="grid gap-5 sm:grid-cols-2 xl:grid-cols-3" id="product-grid">
{cards or '<p class="text-gray-500 col-span-full">表示できる商品がありません。</p>'}
</div>
</main>
{filler}
<footer class="max-w-6xl mx-auto px-4 sm:px-6 py-10 text-center text-xs text-gray-500 space-y-2">
<p>※ 価格は取得時点（{html.escape(updated)}）のものです。購入前に各ショップの表示価格・送料・在庫を必ずご確認ください。</p>
<p>※ 単位価格は価格÷内容量で算出した客観指標、1回あたりは標準使用量に基づく目安です。</p>
<p>※ 当ページのリンクはアフィリエイト広告を含みます。</p></footer>
{CATEGORY_FILTER_SCRIPT}
</body></html>""",
        encoding="utf-8",
    )


def site_url() -> str:
    """公開サイトURL (.env の SITE_URL、未設定時は GitHub Pages 既定値)。"""
    return (
        env("SITE_URL")
        or "https://minnaotokuni.github.io/detergent-price-compare/"
    ).strip()


def _cheapest_shop_label(snapshot: TodaySnapshot) -> tuple[str, str]:
    """3店比較の最安店名と1回あたり価格表示（外部API・LLM不使用、スナップショットのみ）。"""
    offer = snapshot.cheapest_offer
    if offer and offer.price_per_use is not None:
        shop = offer.shop_label
        if shop.startswith("代替:"):
            shop = shop.replace("代替:", "").strip() or "楽天市場"
        unit = offer.use_unit_label or "1回"
        return shop, f"¥{offer.price_per_use:.1f}/{unit}"
    r24 = snapshot.offers.get(RAKUTEN24.key) or snapshot.visible_offers.get(RAKUTEN24.key)
    if r24 and r24.price_per_use is not None:
        unit = r24.use_unit_label or "1回"
        return RAKUTEN24.label, f"¥{r24.price_per_use:.1f}/{unit}"
    return RAKUTEN24.label, "価格確認"


def fit_x_char_limit(text: str, limit: int = X_CHAR_LIMIT) -> str:
    """Xの文字数上限に収める（超過時は while で語単位→末尾省略）。"""
    compact = re.sub(r"[ \t]+", " ", text.replace("\r\n", "\n").strip())
    compact = re.sub(r"\n{3,}", "\n\n", compact)
    trimmed = compact
    while len(trimmed) > limit:
        if " " in trimmed:
            trimmed = trimmed.rsplit(" ", 1)[0].strip()
            continue
        trimmed = trimmed[: max(0, limit - 1)].rstrip() + "…"
        break
    return trimmed


def digest_hashtags() -> str:
    """まとめ投稿末尾のハッシュタグ3つ（.env の DIGEST_HASHTAGS で上書き可）。"""
    return (env("DIGEST_HASHTAGS") or DIGEST_DEFAULT_HASHTAGS).strip()


def strip_digest_hashtags(text: str) -> str:
    """本文から #タグ を除去（末尾はコードで統一付与）。"""
    without = re.sub(r"#\S+", "", text)
    return re.sub(r"\s+", " ", without).strip()


def digest_trailing_suffix() -> str:
    """URL + ハッシュタグ3つ（常にこの順）。"""
    return f"{site_url()} {digest_hashtags()}"


def ensure_site_url_in_digest(text: str, limit: int = X_CHAR_LIMIT) -> str:
    """まとめ投稿に URL とハッシュタグ3つを必ず付与（LLMが落としてもコードで保証）。"""
    url = site_url()
    tags = digest_hashtags()
    body = strip_digest_hashtags(text.replace("\r\n", " "))
    if url in body:
        body = body.split(url, 1)[0].strip()
    suffix = digest_trailing_suffix()
    if not body:
        return suffix if len(suffix) <= limit else suffix[:limit]
    sep = " "
    combined = f"{body}{sep}{suffix}"
    if len(combined) <= limit:
        return combined
    max_body = limit - len(sep) - len(suffix)
    trimmed = body
    while len(trimmed) > max_body and trimmed:
        if " " in trimmed:
            trimmed = trimmed.rsplit(" ", 1)[0].strip()
        else:
            trimmed = trimmed[: max(0, max_body - 1)].rstrip() + "…"
            break
    return f"{trimmed}{sep}{suffix}"


def finalize_digest_tweet(text: str) -> str:
    """140字以内・サイトURL・ハッシュタグ3つ必須のまとめ文に仕上げる。"""
    suffix = digest_trailing_suffix()
    with_meta = ensure_site_url_in_digest(text)
    if site_url() not in with_meta or not all(tag in with_meta for tag in digest_hashtags().split()):
        with_meta = ensure_site_url_in_digest(strip_digest_hashtags(text))
    while len(with_meta) > X_CHAR_LIMIT:
        body = strip_digest_hashtags(with_meta.split(site_url(), 1)[0])
        with_meta = ensure_site_url_in_digest(body[: max(0, X_CHAR_LIMIT - len(suffix) - 2)])
    return with_meta


def rakuten24_summary_line(analysis: ProductAnalysis) -> str:
    """LLM素材用: 楽天24の1回あたり単価と買い時シグナル。"""
    r24 = analysis.snapshot.offers.get(RAKUTEN24.key) or analysis.snapshot.visible_offers.get(
        RAKUTEN24.key
    )
    name = analysis.snapshot.target.display_name
    if r24 and r24.price_per_use is not None:
        unit = r24.use_unit_label or "1回"
        return f"{name} | 楽天24 ¥{r24.price_per_use:.1f}/{unit} | {analysis.signal.label}"
    if r24 and r24.price is not None:
        return f"{name} | 楽天24 本体¥{r24.price:,} | {analysis.signal.label}"
    return f"{name} | 楽天24価格未取得 | {analysis.signal.label}"


def build_digest_material(analyses: list[ProductAnalysis], slot_label: str) -> str:
    """全商品の価格・買い時をカテゴリ別にまとめたLLM入力素材（Pythonのみ・無料）。"""
    lines = [
        f"配信枠: {slot_label}",
        f"対象商品数: {len(analyses)}",
        f"送料無料ライン: ¥{FREE_SHIPPING_THRESHOLD:,}",
        f"比較サイトURL: {site_url()}",
        "",
    ]
    hot = [
        a
        for a in analyses
        if "最安" in a.signal.label or "買い時" in a.signal.label
    ]
    if hot:
        lines.append("【今すぐ買い推奨】")
        lines.extend(rakuten24_summary_line(a) for a in hot)
        lines.append("")

    by_category: dict[str, list[ProductAnalysis]] = {}
    for analysis in analyses:
        cat = CATEGORIES.get(analysis.snapshot.target.category_key)
        cat_name = cat.display_name if cat else analysis.snapshot.target.category_key
        by_category.setdefault(cat_name, []).append(analysis)

    for cat_name, items in by_category.items():
        lines.append(f"■ {cat_name} ({len(items)}品)")
        for analysis in items:
            lines.append(rakuten24_summary_line(analysis))
        lines.append("")

    wait = [a for a in analyses if "待ち" in a.signal.label or "取得待ち" in a.signal.label]
    if wait:
        lines.append(f"【価格取得待ち {len(wait)}品】")
        for analysis in wait[:5]:
            lines.append(f"- {analysis.snapshot.target.display_name}")
    return "\n".join(lines).strip()


def ollama_base_url() -> str:
    return (env("OLLAMA_BASE_URL") or "http://127.0.0.1:11434").rstrip("/")


def ollama_is_running() -> bool:
    try:
        response = requests.get(f"{ollama_base_url()}/api/tags", timeout=3)
        return response.ok
    except requests.RequestException:
        return False


def _llm_provider() -> str:
    explicit = (env("LLM_PROVIDER") or "").strip().lower()
    if explicit == "ollama":
        return "ollama" if ollama_is_running() else ""
    if explicit in ("openai", "anthropic"):
        return explicit
    if ollama_is_running():
        return "ollama"
    if env("OPENAI_API_KEY"):
        return "openai"
    if env("ANTHROPIC_API_KEY"):
        return "anthropic"
    return ""


def call_digest_llm(material: str, slot_label: str) -> str:
    """LLMでまとめ投稿文を生成（schedule 専用。1日2回のみ。Ollama ローカル優先）。"""
    provider = _llm_provider()
    if not provider:
        raise SystemExit(
            "LLM が使えません。Ollama を起動するか、.env に OPENAI_API_KEY / "
            "ANTHROPIC_API_KEY を設定してください。"
            " （Ollama: ollama serve → LLM_PROVIDER=ollama）"
        )
    url = site_url()
    user_prompt = (
        f"{DIGEST_LLM_INSTRUCTION}\n\n"
        f"【必須】システムが末尾に付ける: URL={url} ハッシュタグ={digest_hashtags()} "
        f"（本文にはURL・#タグを書かない）\n\n"
        f"---\n入力データ:\n{material}\n---\n"
        f"配信タイミング: {slot_label}"
    )
    system_prompt = (
        "あなたは日用品のバイヤー兼SNS編集者です。日本語で短く刺さるコピーを書きます。"
        f" 本文のみ書き、URLと {digest_hashtags()} はシステムが付けます。"
    )

    if provider == "ollama":
        model = env("OLLAMA_MODEL") or "gemma2:9b"
        response = requests.post(
            f"{ollama_base_url()}/api/chat",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
                "options": {"temperature": 0.7, "num_predict": 256},
            },
            timeout=180,
        )
        response.raise_for_status()
        payload = response.json()
        raw = (payload.get("message") or {}).get("content", "").strip()
        if not raw:
            raise ValueError("Ollama が空の応答を返しました")
    elif provider == "anthropic":
        api_key = env("ANTHROPIC_API_KEY", required=True)
        model = env("ANTHROPIC_MODEL") or "claude-3-5-haiku-20241022"
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 256,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            },
            timeout=90,
        )
        response.raise_for_status()
        payload = response.json()
        parts = payload.get("content") or []
        raw = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
    else:
        api_key = env("OPENAI_API_KEY", required=True)
        model = env("OPENAI_MODEL") or "gpt-4o-mini"
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": 256,
                "temperature": 0.7,
            },
            timeout=90,
        )
        response.raise_for_status()
        payload = response.json()
        raw = payload["choices"][0]["message"]["content"].strip()

    raw = raw.strip().strip('"').strip("「").strip("」")
    return finalize_digest_tweet(raw)


def build_digest_tweet_fallback(analyses: list[ProductAnalysis], slot_label: str) -> str:
    """LLM不可時のローカルまとめ（dry-run確認用フォールバック）。"""
    url = site_url()
    hot = [
        a
        for a in analyses
        if "最安" in a.signal.label or "買い時" in a.signal.label
    ][:4]
    picks = hot or analyses[:4]
    header = f"【{slot_label}まとめ】"
    suffix = f"全{len(analyses)}品→{url}"
    item_bits: list[str] = []
    budget = X_CHAR_LIMIT - len(header) - len(suffix) - 2
    for analysis in picks:
        name = analysis.snapshot.target.display_name
        if len(name) > 10:
            name = name[:9] + "…"
        r24 = analysis.snapshot.offers.get(RAKUTEN24.key)
        if r24 and r24.price_per_use is not None:
            bit = f"{name}¥{r24.price_per_use:.0f}"
        elif r24 and r24.price is not None:
            bit = f"{name}¥{r24.price}"
        else:
            continue
        sep = 1 if item_bits else 0
        if sum(len(b) for b in item_bits) + sep + len(bit) > budget:
            break
        item_bits.append(bit)
    body = " ".join([header, *item_bits, suffix])
    return finalize_digest_tweet(body)


def generate_digest_tweet(
    analyses: list[ProductAnalysis],
    slot_id: str,
    slot_label: str,
    *,
    use_llm: bool,
) -> str:
    material = build_digest_material(analyses, slot_label)
    if use_llm:
        try:
            return call_digest_llm(material, slot_label)
        except (requests.RequestException, KeyError, IndexError, ValueError) as e:
            print(f"[WARN] LLM生成失敗→ローカルフォールバック: {e}", file=sys.stderr)
    return build_digest_tweet_fallback(analyses, slot_label)


def tweet_text(analysis: ProductAnalysis) -> str:
    """単品投稿用（post コマンド）。ローカル f-string のみ。"""
    signal = analysis.signal.label
    name = analysis.snapshot.target.display_name
    shop, price = _cheapest_shop_label(analysis.snapshot)
    text = (
        f"【{signal}】\n"
        f"{name} は今日 {shop} が真の最安 {price}。\n"
        f"洗剤の買い時比較はプロフィールからチェック！"
    )
    while len(text) > X_CHAR_LIMIT and len(name) > 6:
        name = name[:-2] + "…"
        text = (
            f"【{signal}】\n"
            f"{name} は今日 {shop} が真の最安 {price}。\n"
            f"洗剤の買い時比較はプロフィールからチェック！"
        )
    return text[:X_CHAR_LIMIT]




def is_oauth1_access_token(token: str) -> bool:
    """OAuth 1.0a の Access Token は ``数字-英数字`` 形式 (例: 1234567890-xxxxxxxx)."""
    return bool(re.match(r"^\d+-[A-Za-z0-9]+$", token.strip()))


class XPoster:
    """X へ投稿。tweepy は OAuth 1.0a User Context が最も安定 (Free プラン可)。"""

    def __init__(self) -> None:
        if tweepy is None:
            raise SystemExit("tweepy が未インストールです。")
        api_key = env("X_API_KEY", required=True)
        api_secret = env("X_API_SECRET", required=True)
        access_token = env("X_ACCESS_TOKEN", required=True)
        access_secret = env("X_ACCESS_TOKEN_SECRET")

        if is_oauth1_access_token(access_token):
            if not access_secret:
                raise SystemExit(
                    "X_ACCESS_TOKEN_SECRET が未設定です。"
                    " OAuth 1.0a では Access Token と Secret のペアが必要です。"
                )
            self.client = tweepy.Client(
                consumer_key=api_key,
                consumer_secret=api_secret,
                access_token=access_token,
                access_token_secret=access_secret,
            )
            self._auth_mode = "oauth1"
            return

        # OAuth 2.0 User Access Token (base64 で ``ユーザーID:1:ci`` 等) が .env に入っているケース
        self.client = tweepy.Client(
            consumer_key=api_key,
            consumer_secret=api_secret,
            access_token=access_token,
        )
        self._auth_mode = "oauth2"

    def post(self, text: str) -> str:
        try:
            response = self.client.create_tweet(text=text)
        except tweepy.Unauthorized as e:
            if self._auth_mode == "oauth2":
                raise SystemExit(
                    "X API 401: .env の X_ACCESS_TOKEN は OAuth 2.0 形式ですが、"
                    "認証に失敗しました（期限切れ・スコープ不足の可能性）。\n"
                    "developer.x.com → 対象アプリ → Keys and tokens → "
                    "「Access Token and Secret」(OAuth 1.0a) を Read and write で再生成し、\n"
                    "``1234567890-xxxxxxxx`` 形式の Token と Secret を X_ACCESS_TOKEN / "
                    "X_ACCESS_TOKEN_SECRET に貼り直してください。\n"
                    "（OAuth 2.0 のトークンはこのツールの X_ACCESS_TOKEN 欄には使えません）"
                ) from e
            raise SystemExit(
                "X API 401: API Key / Secret / Access Token のいずれかが無効です。\n"
                "権限を Read and write にしたうえで、Access Token と Secret を再生成してください。"
            ) from e
        return str((getattr(response, "data", None) or {}).get("id") or "")


def load_post_log(path: Path = DEFAULT_POST_LOG) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_post_log(log: dict, path: Path = DEFAULT_POST_LOG) -> None:
    try:
        path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def digest_log_key(slot_id: str) -> str:
    return f"digest_{slot_id}"


def current_digest_slot(now: Optional[datetime] = None) -> Optional[tuple[str, str, int]]:
    """7時・20時の投稿枠内なら (slot_id, 表示名, 時) を返す（cron 用・±25分）。"""
    now = now or datetime.now()
    for slot_id, label, hour in SCHEDULE_SLOTS:
        if now.hour == hour and now.minute < 25:
            return slot_id, label, hour
    return None


def next_digest_wakeup(now: Optional[datetime] = None) -> tuple[str, str, int, datetime]:
    """次に投稿すべき枠とその日時。"""
    now = now or datetime.now()
    upcoming: list[tuple[datetime, str, str, int]] = []
    for slot_id, label, hour in SCHEDULE_SLOTS:
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        upcoming.append((target, slot_id, label, hour))
    upcoming.sort(key=lambda item: item[0])
    target, slot_id, label, hour = upcoming[0]
    return slot_id, label, hour, target


def schedule_use_llm(*, dry_run: bool, no_llm: bool) -> bool:
    if no_llm:
        return False
    if dry_run:
        return bool(_llm_provider())
    return True


def preview_digest_schedules(analyses: list[ProductAnalysis], *, use_llm: bool) -> None:
    """朝7時・夜20時の2枠ぶん、全商品まとめ投稿文をプレビュー。"""
    if use_llm:
        prov = _llm_provider()
        mode = f"LLM ({prov})" if prov else "LLM"
    else:
        mode = "テンプレート"
    print(
        f"[INFO] まとめ投稿プレビュー {len(analyses)}商品 / 2枠（{mode}）",
        file=sys.stderr,
    )
    for slot_id, label, hour in SCHEDULE_SLOTS:
        slot_label = f"{label}枠"
        text = generate_digest_tweet(analyses, slot_id, slot_label, use_llm=use_llm)
        print(f"\n{'═' * 44}")
        print(f"【{slot_id}】 {label} ({hour}:00) — {len(text)}/{X_CHAR_LIMIT}字")
        print(f"{'═' * 44}\n{text}\n")


def post_digest_slot(
    analyses: list[ProductAnalysis],
    slot_id: str,
    slot_label: str,
    *,
    dry_run: bool,
    use_llm: bool,
) -> bool:
    """1枠ぶんのまとめ投稿。成功・dry-run 実行時 True。"""
    today_key = date.today().isoformat()
    log = load_post_log()
    today_log = log.setdefault(today_key, {})
    key = digest_log_key(slot_id)
    if today_log.get(key, "").startswith("posted"):
        print(f"[INFO] 本日投稿済み: {key}", file=sys.stderr)
        return False
    text = finalize_digest_tweet(
        generate_digest_tweet(analyses, slot_id, slot_label, use_llm=use_llm)
    )
    if site_url() not in text:
        raise SystemExit("投稿文にサイトURLを含められませんでした。SITE_URL を確認してください。")
    for tag in digest_hashtags().split():
        if tag not in text:
            raise SystemExit(f"投稿文に {tag} が含まれていません。DIGEST_HASHTAGS を確認してください。")
    print("----\n" + text + f"\n---- ({len(text)}/{X_CHAR_LIMIT}字)")
    if dry_run:
        today_log[key] = "dry-run"
        save_post_log(log)
        return True
    try:
        tweet_id = XPoster().post(text)
        today_log[key] = f"posted:{tweet_id}"
        print(f"[OK] まとめ投稿完了 ({slot_id}): {tweet_id}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        today_log[key] = f"failed:{type(e).__name__}"
        print(f"[WARN] 投稿失敗: {e}", file=sys.stderr)
        save_post_log(log)
        return False
    save_post_log(log)
    return True


def post_due_digest(
    analyses: list[ProductAnalysis],
    *,
    dry_run: bool,
    use_llm: bool,
) -> bool:
    """cron 向け: 7時または20時枠のときだけまとめ1件投稿。"""
    slot = current_digest_slot()
    if slot is None:
        now = datetime.now()
        hours = ", ".join(str(h) for _, _, h in SCHEDULE_SLOTS)
        print(
            f"[INFO] 投稿枠外 (現在 {now.hour}:{now.minute:02d}, 枠は {hours}時台のみ)",
            file=sys.stderr,
        )
        return False
    slot_id, label, _hour = slot
    return post_digest_slot(
        analyses,
        slot_id,
        f"{label}枠",
        dry_run=dry_run,
        use_llm=use_llm,
    )


def run_schedule_digest(analyses: list[ProductAnalysis], *, dry_run: bool, use_llm: bool) -> None:
    """次の朝7時 or 夜20時まで待機し、まとめ1件投稿して終了（1日2回は cron で2回起動）。"""
    if dry_run:
        preview_digest_schedules(analyses, use_llm=use_llm)
        return
    slot_id, label, hour, target = next_digest_wakeup()
    wait_sec = (target - datetime.now()).total_seconds()
    if wait_sec > 0:
        print(
            f"[INFO] 次の投稿枠 {label} ({hour}:00) まで {int(wait_sec // 60)}分待機…",
            file=sys.stderr,
        )
        time.sleep(wait_sec)
    post_digest_slot(
        analyses,
        slot_id,
        f"{label}枠",
        dry_run=False,
        use_llm=use_llm,
    )


def collect_and_analyze(append_csv: bool) -> list[ProductAnalysis]:
    ensure_history_csv()
    history_before = read_history()
    snapshots = collect_snapshots(load_targets())
    analyses = analyze(snapshots, history_before)
    if append_csv:
        append_history(snapshots)
    return analyses


def analyses_from_latest_history() -> list[ProductAnalysis]:
    rows = read_history()
    if not rows:
        return []
    latest = max(row["日付"] for row in rows)
    latest_by_jan: dict[str, dict[str, str]] = {}
    for row in rows:
        if row["日付"] == latest:
            latest_by_jan[row["JANコード"]] = row
    latest_rows = list(latest_by_jan.values())
    history_before = [row for row in rows if row["日付"] != latest]
    targets = {t.jan: t for t in load_targets()}
    analyses: list[ProductAnalysis] = []
    for row in latest_rows:
        target = targets.get(row["JANコード"])
        if target is None:
            continue
        offers = {}
        category = CATEGORIES.get(target.category_key)
        unit_label = category.use_unit_label if category else ""
        for shop in SHOPS:
            offer = ShopOffer(
                shop_key=shop.key,
                shop_label=shop.label,
                price=parse_price(row.get(shop.csv_column)),
                price_per_use=parse_per_use(row.get(shop.per_use_csv_column)),
                use_unit_label=unit_label,
            )
            enrich_offer(offer, target, shop)
            offers[shop.key] = offer
        snapshot = TodaySnapshot(target, offers, dict(offers))
        stats = calculate_stats(history_before, target.jan)
        r24 = offers.get(RAKUTEN24.key)
        analyses.append(
            ProductAnalysis(
                snapshot,
                stats,
                judge_signal(r24.price if r24 else None, stats),
                unit_price=r24.unit_price if r24 else None,
                unit_basis=(r24.unit_basis if r24 else "") or unit_basis_label(target.size_unit),
            )
        )
    assign_category_ranks(analyses)
    return analyses


def cmd_collect(_args: argparse.Namespace) -> None:
    analyses = collect_and_analyze(append_csv=True)
    print(f"価格履歴を追記しました: {PRICE_HISTORY_CSV} ({len(analyses)}商品)")


def cmd_html(args: argparse.Namespace) -> None:
    # デフォルトでAPI再取得（CSVだけだと空欄が多くサイトとして成立しないため）
    analyses = analyses_from_latest_history() if args.no_fetch else collect_and_analyze(append_csv=False)
    generate_html(analyses, args.out)
    priced = sum(
        1
        for a in analyses
        if (a.snapshot.visible_offers.get(RAKUTEN24.key) or ShopOffer("", "")).price_per_use is not None
        or (a.snapshot.visible_offers.get(RAKUTEN24.key) or ShopOffer("", "")).price is not None
    )
    print(f"HTML生成: {args.out} ({len(analyses)}商品, 楽天24価格あり {priced}件)")


def cmd_refresh(args: argparse.Namespace) -> None:
    analyses = collect_and_analyze(append_csv=True)
    generate_html(analyses, args.out)
    print(f"履歴追記 + HTML生成完了: {args.out} ({len(analyses)}商品)")


def cmd_schedule(args: argparse.Namespace) -> None:
    use_csv = args.no_fetch or args.dry_run
    analyses = analyses_from_latest_history() if use_csv else collect_and_analyze(append_csv=False)
    if not analyses:
        raise SystemExit(
            "分析対象がありません。先に python main.py refresh を実行するか、"
            " --no-fetch を外して API 取得してください。"
        )
    use_llm = schedule_use_llm(dry_run=args.dry_run, no_llm=args.no_llm)
    if args.dry_run and not use_llm:
        print(
            "[WARN] LLM未使用のためテンプレートでプレビューします。"
            " Ollama を起動するか --no-llm を外してください。",
            file=sys.stderr,
        )
    elif use_llm and _llm_provider() == "ollama":
        model = env("OLLAMA_MODEL") or "gemma2:9b"
        print(f"[INFO] ローカル LLM: Ollama ({model})", file=sys.stderr)
    run_schedule_digest(analyses, dry_run=args.dry_run, use_llm=use_llm)


def cmd_post_due(args: argparse.Namespace) -> None:
    """7時・20時枠のまとめ投稿（cron / launchd 向け・1回実行で終了）。"""
    analyses = analyses_from_latest_history() if args.no_fetch else collect_and_analyze(append_csv=False)
    if not analyses:
        raise SystemExit("分析対象がありません。先に python main.py refresh を実行してください。")
    use_llm = schedule_use_llm(dry_run=args.dry_run, no_llm=args.no_llm)
    post_due_digest(analyses, dry_run=args.dry_run, use_llm=use_llm)


def cmd_post(args: argparse.Namespace) -> None:
    # 投稿文は1回あたり価格が必要なので、デフォルトで現在価格を再取得する。
    analyses = analyses_from_latest_history() if args.no_fetch else collect_and_analyze(append_csv=False)
    analysis = next((a for a in analyses if a.snapshot.target.id == args.target_id), None)
    if analysis is None:
        raise SystemExit("target_id が見つかりません。")
    text = tweet_text(analysis)
    print(text)
    if not args.dry_run:
        print("投稿成功:", XPoster().post(text))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="価格履歴CSV蓄積・買い時判定・HTML生成・X投稿ツール")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("collect", help="価格取得して price_history.csv に追記")
    sp.set_defaults(func=cmd_collect)
    sp = sub.add_parser("html", help="index.html を生成（デフォルトで現在価格を再取得）")
    sp.add_argument("--out", type=Path, default=DEFAULT_HTML_PATH)
    sp.add_argument("--no-fetch", action="store_true", help="CSV最新行のみで生成（API再取得しない）")
    sp.add_argument("--fetch", action="store_true", help=argparse.SUPPRESS)  # 後方互換
    sp.set_defaults(func=cmd_html)
    sp = sub.add_parser("refresh", help="価格取得 → CSV追記 → HTML生成")
    sp.add_argument("--out", type=Path, default=DEFAULT_HTML_PATH)
    sp.set_defaults(func=cmd_refresh)
    sp = sub.add_parser(
        "schedule",
        help="朝7時・夜20時のまとめ投稿（--dry-run で2枠プレビュー、本番は次枠まで待って1件投稿）",
    )
    sp.add_argument("--no-fetch", action="store_true", help="現在価格を再取得せずCSV最新行から素材を作る")
    sp.add_argument(
        "--dry-run",
        action="store_true",
        help="朝・夜の2枠ぶんまとめ文を生成して表示（LLM使用・X投稿なし）",
    )
    sp.add_argument(
        "--no-llm",
        action="store_true",
        help="LLMを使わずローカルフォールバック文面のみ生成",
    )
    sp.set_defaults(func=cmd_schedule)
    sp = sub.add_parser(
        "post-due",
        help="7時 or 20時枠のときだけ全商品まとめを1件Xへ投稿（cron 推奨）",
    )
    sp.add_argument("--no-fetch", action="store_true", help="CSV最新行のみで素材を作る")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--no-llm", action="store_true", help="LLMを使わずローカルフォールバック文面")
    sp.set_defaults(func=cmd_post_due)
    sp = sub.add_parser("post", help="指定商品を1件だけXへ投稿")
    sp.add_argument("--target-id", required=True)
    sp.add_argument("--no-fetch", action="store_true", help="現在価格を再取得せずCSV最新行から投稿文を作る")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_post)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
