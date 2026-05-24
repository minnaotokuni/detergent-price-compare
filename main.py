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


@dataclass(frozen=True)
class BuySignal:
    label: str
    tone: str
    description: str


@dataclass(frozen=True)
class ProductAnalysis:
    snapshot: TodaySnapshot
    stats: PriceStats
    signal: BuySignal


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
    """楽天24の1回あたり単価履歴を基準に買い時統計を算出する。"""
    prices = [
        price
        for row in history_rows
        if row.get("JANコード") == jan
        for price in [parse_per_use(row.get(RAKUTEN24.per_use_csv_column))]
        if price is not None
    ]
    if not prices:
        return PriceStats(None, None)
    return PriceStats(min(prices), sum(prices) / len(prices))


def judge_signal(today_per_use: Optional[float], stats: PriceStats) -> BuySignal:
    if today_per_use is None:
        return BuySignal("価格取得待ち", "gray", "楽天24の価格を取得できませんでした。後ほど再確認してください。")
    past_min = stats.past_min if stats.past_min is not None else today_per_use
    past_avg = stats.past_avg if stats.past_avg is not None else today_per_use
    if today_per_use <= past_min:
        return BuySignal("激アツ！過去最安値", "red", "楽天24の1回あたり単価が履歴底値。まとめ買いチャンスです。")
    if today_per_use <= past_avg:
        return BuySignal("おすすめ！買い時", "green", "楽天24の1回あたり単価が過去平均以下。カゴに入れてOK。")
    return BuySignal("今は待て！高値傾向", "slate", "楽天24は過去平均より高め。急ぎでなければ次のセール待ち。")


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
        targets.append(
            TargetProduct(
                id=str(entry.get("id") or jan),
                display_name=str(entry.get("display_name") or entry.get("search_keyword") or jan),
                search_keyword=str(entry.get("search_keyword") or entry.get("display_name") or jan),
                category_key=str(entry.get("category_key") or "laundry_liquid"),
                jan=jan,
                total_shares=total_shares,
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
        return {"Referer": ref, "Origin": ref.rstrip("/")}

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


def analyze(snapshots: list[TodaySnapshot], history_rows: list[dict[str, str]]) -> list[ProductAnalysis]:
    results = []
    for snapshot in snapshots:
        stats = calculate_stats(history_rows, snapshot.target.jan)
        r24 = snapshot.visible_offers.get(RAKUTEN24.key) or snapshot.offers.get(RAKUTEN24.key)
        today = r24.price_per_use if r24 else None
        results.append(ProductAnalysis(snapshot, stats, judge_signal(today, stats)))
    return results


def amazon_url(target: TargetProduct) -> str:
    q = urllib.parse.quote(target.jan or target.display_name)
    url = f"https://www.amazon.co.jp/s?k={q}"
    if env("AMAZON_ASSOCIATE_TAG"):
        url += f"&tag={urllib.parse.quote(env('AMAZON_ASSOCIATE_TAG'))}"
    return url


def fmt_price(price: Optional[int]) -> str:
    return f"¥{price:,}" if price is not None else "—"


def fmt_offer_price(offer: ShopOffer) -> str:
    if offer.price_per_use is not None:
        return f"¥{offer.price_per_use:.1f}"
    if offer.price is not None:
        return fmt_price(offer.price)
    return "—"


def fmt_hero_price(offer: ShopOffer) -> str:
    """カード主役エリア用。1回あたり単価を優先し、なければ本体価格を表示。"""
    if offer.price_per_use is not None:
        return f"¥{offer.price_per_use:.1f}"
    if offer.price is not None:
        return fmt_price(offer.price)
    return "—"


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
<section class="max-w-6xl mx-auto px-4 sm:px-6 pb-2">
  <div class="flex gap-2 overflow-x-auto pb-2 -mx-1 px-1 scrollbar-hide" id="category-tabs" role="tablist">
    {''.join(buttons)}
  </div>
  <p class="text-xs text-gray-400 mt-1" id="category-tab-count"></p>
</section>
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


def rakuten24_is_today_best(snapshot: TodaySnapshot) -> bool:
    """3店舗公式取得分で楽天24の1回あたり単価が最安または同額か。"""
    r24 = snapshot.offers.get(RAKUTEN24.key)
    if not r24 or r24.price_per_use is None:
        return False
    rivals: list[float] = []
    for shop in SHOPS[1:]:
        offer = snapshot.offers.get(shop.key)
        if offer and offer.price_per_use is not None:
            rivals.append(offer.price_per_use)
    if not rivals:
        return True
    return r24.price_per_use <= min(rivals)


def render_competitor_block(shop: ShopConfig, offer: ShopOffer) -> str:
    is_fallback = offer.shop_label != shop.label
    label = offer.shop_label if is_fallback else shop.label
    fallback_badge = (
        '<span class="ml-1 text-[10px] px-1.5 py-0.5 rounded bg-amber-100 text-amber-700">代替</span>'
        if is_fallback
        else ""
    )
    return (
        f'<div class="rounded-xl border border-gray-200 bg-gray-50/80 p-3">'
        f'<p class="text-[11px] text-gray-500 font-medium">{html.escape(label)}{fallback_badge}</p>'
        f'<p class="text-lg font-bold text-gray-700 tabular-nums mt-1">{fmt_offer_price(offer)}</p>'
        f'<p class="text-[10px] text-gray-400 mt-1 leading-snug">{html.escape(offer_detail(offer))}</p>'
        f"</div>"
    )


def button(label: str, url: str, classes: str, disabled: bool = False) -> str:
    if disabled or not url:
        return f'<span class="text-center text-xs px-3 py-2 rounded-lg bg-gray-200 text-gray-400">{html.escape(label)}</span>'
    return (
        f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="nofollow sponsored noopener" '
        f'class="text-center text-xs px-3 py-2 rounded-lg font-bold text-white shadow-sm hover:opacity-90 {classes}">'
        f'{html.escape(label)}</a>'
    )


def render_buyer_advice_section() -> str:
    return """
<section class="max-w-6xl mx-auto px-4 sm:px-6 py-8">
  <div class="rounded-3xl bg-gradient-to-br from-red-600 via-red-500 to-orange-500 text-white p-6 sm:p-8 shadow-xl">
    <p class="text-xs font-black tracking-widest uppercase opacity-90">Buyer Strategy / 仕入れバイヤーの視点</p>
    <h2 class="text-xl sm:text-2xl font-black mt-2 leading-snug">単品最安に騙されるな。トータルコストで勝て。</h2>
    <div class="mt-5 grid gap-4 sm:grid-cols-2">
      <motion class="rounded-2xl bg-white/15 backdrop-blur-sm border border-white/20 p-4">
        <p class="text-sm font-bold mb-2">💡 まとめ買いの正解は楽天24</p>
        <p class="text-sm leading-relaxed opacity-95">他店が100円安くても、店ごとに3,980円の送料ラインを埋めるのは至難の業。品揃え日本最大級の<strong class="font-black">楽天24</strong>で日用品を一発まとめ買いし、送料無料にするのがタイパもコスパも最強です。</p>
      </motion>
      <motion class="rounded-2xl bg-white/15 backdrop-blur-sm border border-white/20 p-4">
        <p class="text-sm font-bold mb-2">📊 今日の楽天24は本当に安い？</p>
        <p class="text-sm leading-relaxed opacity-95">当サイトは容量から割り出した<strong class="font-black">1回あたり真の単価</strong>で、楽天24の今日の値段が他店・過去履歴と比べて本当にお得かをリアルタイム判定します。</p>
      </motion>
    </motion>
    <p class="text-xs mt-4 opacity-80">送料無料ライン: ¥3,980〜 ｜ 比較対象: 楽天24・サンドラッグ・爽快ドラッグ</p>
  </motion>
</section>
""".strip().replace("<motion", "<div").replace("</motion>", "</div>")


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
    r24 = snapshot.visible_offers.get(RAKUTEN24.key) or ShopOffer(RAKUTEN24.key, RAKUTEN24.label)
    unit = r24.use_unit_label or "1回"
    is_best = rakuten24_is_today_best(snapshot)
    best_badge = (
        '<motion class="mt-3 inline-flex items-center gap-2 rounded-xl bg-red-600 text-white px-4 py-2 text-sm font-black shadow-md">'
        "🏆 楽天24が本日最安値！ここでまとめ買い決定"
        "</motion>"
        if is_best
        else ""
    )
    r24_fallback = r24.shop_label != RAKUTEN24.label
    r24_cta = "楽天24でまとめ買い" if not r24_fallback else "代替候補を見る"
    competitors: list[str] = []
    comp_buttons: list[str] = []
    for shop in SHOPS[1:]:
        offer = snapshot.visible_offers.get(shop.key) or ShopOffer(shop.key, shop.label)
        competitors.append(render_competitor_block(shop, offer))
        comp_label = "代替候補" if offer.shop_label != shop.label else shop.label
        comp_buttons.append(button(f"{comp_label}で比較", offer.url, shop.button_class, not offer.url))
    card = f"""
<article class="product-card bg-white rounded-2xl shadow-md border border-gray-100 overflow-hidden flex flex-col" data-category="{html.escape(category_tab_key(snapshot.target.category_key))}">
  <header class="px-5 pt-5 pb-3 flex items-start justify-between gap-3 border-b border-gray-50">
    <div>
      <p class="text-[11px] text-gray-400 font-mono">JAN: {html.escape(snapshot.target.jan)}</p>
      <h2 class="text-lg font-extrabold text-gray-900 leading-snug">{html.escape(snapshot.target.display_name)}</h2>
      <p class="text-[10px] text-red-500 font-bold mt-0.5">{html.escape(CATEGORIES.get(snapshot.target.category_key).display_name if CATEGORIES.get(snapshot.target.category_key) else snapshot.target.category_key)}</p>
    </div>
    <span class="shrink-0 text-xs font-bold px-3 py-1 rounded-full border {signal_class(analysis.signal.tone)}">{html.escape(analysis.signal.label)}</span>
  </header>
  <div class="bg-gradient-to-br from-red-50 via-white to-orange-50 p-5 border-b-4 border-red-500">
    <div class="flex items-center gap-2 mb-2 flex-wrap">
      <span class="text-xs font-black tracking-wider text-red-600 uppercase">楽天24 — まとめ買い主軸</span>
      <span class="text-[10px] px-2 py-0.5 rounded-full bg-red-100 text-red-700 font-bold">送料無料ライン {FREE_SHIPPING_THRESHOLD:,}円</span>
    </div>
    <p class="text-xs text-gray-500">楽天24 今日の1回あたり ({html.escape(unit)})</p>
    <p class="text-4xl font-black text-red-700 tabular-nums mt-1">{fmt_hero_price(r24)}</p>
    {f'<p class="text-xs text-amber-700 mt-1">※ 本体価格表示（{fmt_price(r24.price)}）容量推定できず</p>' if r24.price is not None and r24.price_per_use is None else ''}
    <p class="text-sm text-gray-700 mt-2 leading-relaxed">{html.escape(offer_detail(r24))}</p>
    <p class="text-sm font-medium text-gray-800 mt-3">{html.escape(analysis.signal.description)}</p>
    {best_badge}
    <div class="grid grid-cols-2 gap-2 mt-4 text-xs">
      <div class="rounded-lg bg-white/80 border border-red-100 px-3 py-2">楽天24 過去最安: <b class="text-red-700">{fmt_per_use(analysis.stats.past_min)}</b></div>
      <div class="rounded-lg bg-white/80 border border-red-100 px-3 py-2">楽天24 過去平均: <b class="text-red-700">{fmt_per_use(analysis.stats.past_avg)}</b></div>
    </div>
    <div class="mt-4">{button(r24_cta, r24.url, "bg-red-600 w-full text-sm py-3", not r24.url)}</div>
  </div>
  <motion class="p-4 bg-gray-50/50">
    <p class="text-[11px] font-bold text-gray-400 uppercase tracking-wide mb-2">他店比較（参考）</p>
    <div class="grid grid-cols-2 gap-2">{''.join(competitors)}</div>
    <div class="grid grid-cols-2 gap-2 mt-2">{''.join(comp_buttons)}</div>
    <div class="mt-2">{button("Amazonでも検索", amazon_url(snapshot.target), "bg-amber-500 w-full text-xs py-2")}</div>
  </div>
</article>
""".strip()
    return card.replace("<motion", "<div").replace("</motion>", "</div>")


def generate_html(analyses: list[ProductAnalysis], out: Path = DEFAULT_HTML_PATH) -> None:
    cards = "\n".join(render_card(a) for a in analyses)
    updated = datetime.now().strftime("%Y-%m-%d %H:%M")
    advice = render_buyer_advice_section()
    tabs = render_category_tabs()
    filler = render_filler_section()
    out.write_text(
        f"""<!DOCTYPE html>
<html lang="ja"><head>
<meta charset="utf-8" /><meta name="viewport" content="width=device-width,initial-scale=1" />
<title>日用品まとめ買い トータルコスト最適化 | 楽天24中心3店比較</title>
<meta name="description" content="仕入れバイヤー視点で楽天24まとめ買いのトータルコストを最適化。1回あたり真の単価で買い時を判定します。" />
<script src="https://cdn.tailwindcss.com"></script>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;700;900&family=Noto+Sans+JP:wght@400;700;900&display=swap" rel="stylesheet">
<style>html{{font-family:'Inter','Noto Sans JP',system-ui,sans-serif}}.tabular-nums{{font-variant-numeric:tabular-nums}}</style>
</head><body class="bg-gradient-to-b from-slate-50 to-slate-100 min-h-screen">
<header class="bg-white border-b border-gray-100 shadow-sm"><div class="max-w-6xl mx-auto px-4 sm:px-6 py-6">
<p class="text-xs font-black tracking-widest text-red-600 uppercase">Total Cost Optimizer</p>
<h1 class="text-2xl sm:text-3xl font-black text-gray-900 mt-1">日用品まとめ買い トータルコスト最適化</h1>
<p class="text-sm text-gray-600 mt-2">楽天24を軸に、真の1回あたり単価で買い時を判定するバイヤー視点の比較サイト</p>
<p class="text-xs text-gray-400 mt-2">最終更新: {html.escape(updated)}</p></div></header>
{advice}
{tabs}
<main class="max-w-6xl mx-auto px-4 sm:px-6 py-6">
<h2 class="text-lg font-black text-gray-800 mb-4">日用品 3社比較 ＆ 買い時カード</h2>
<div class="grid gap-5 sm:grid-cols-2 xl:grid-cols-3" id="product-grid">
{cards or '<p class="text-gray-500 col-span-full">表示できる商品がありません。</p>'}
</div>
</main>
{filler}
<footer class="max-w-6xl mx-auto px-4 sm:px-6 py-10 text-center text-xs text-gray-500 space-y-2">
<p>※ 価格は取得時点のものです。購入前に各ショップの表示価格・送料・在庫を確認してください。</p>
<p>※ 当ページのリンクはアフィリエイト広告を含みます。</p></footer>
{CATEGORY_FILTER_SCRIPT}
</body></html>""",
        encoding="utf-8",
    )


def tweet_text(analysis: ProductAnalysis) -> str:
    r24 = analysis.snapshot.visible_offers.get(RAKUTEN24.key) or analysis.snapshot.offers.get(RAKUTEN24.key)
    price = (
        f"¥{r24.price_per_use:.1f}/{r24.use_unit_label or '1回'}"
        if r24 and r24.price_per_use is not None
        else "価格確認"
    )
    name = analysis.snapshot.target.display_name
    text = f"【{analysis.signal.label}】\n{name} は楽天24が {price}。\nまとめ買いの買い時比較はプロフィールから！"
    while len(text) > X_CHAR_LIMIT and len(name) > 6:
        name = name[:-2] + "…"
        text = f"【{analysis.signal.label}】\n{name} は楽天24が {price}。\nまとめ買いの買い時比較はプロフィールから！"
    return text[:X_CHAR_LIMIT]




class XPoster:
    def __init__(self) -> None:
        if tweepy is None:
            raise SystemExit("tweepy が未インストールです。")
        missing = [n for n in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET") if not env(n)]
        if missing:
            raise SystemExit("X 認証情報不足: " + ", ".join(missing))
        self.client = tweepy.Client(
            bearer_token=env("X_BEARER_TOKEN") or None,
            consumer_key=env("X_API_KEY"),
            consumer_secret=env("X_API_SECRET"),
            access_token=env("X_ACCESS_TOKEN"),
            access_token_secret=env("X_ACCESS_TOKEN_SECRET"),
        )

    def post(self, text: str) -> str:
        response = self.client.create_tweet(text=text)
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


def schedule_posts(analyses: list[ProductAnalysis], dry_run: bool, start_hour: int, end_hour: int, interval: int) -> None:
    poster = None if dry_run else XPoster()
    today_key = date.today().isoformat()
    log = load_post_log()
    today_log = log.setdefault(today_key, {})
    base = datetime.now().replace(hour=start_hour, minute=0, second=0, microsecond=0)
    for i, analysis in enumerate(analyses):
        target_time = base + timedelta(minutes=interval * i)
        if target_time.hour >= end_hour:
            break
        target_id = analysis.snapshot.target.id
        if today_log.get(target_id) == "posted":
            continue
        wait_sec = (target_time - datetime.now()).total_seconds()
        if wait_sec > 0:
            time.sleep(wait_sec)
        elif wait_sec < -interval * 60:
            continue
        text = tweet_text(analysis)
        print("----\n" + text + "\n----")
        if dry_run or poster is None:
            today_log[target_id] = "dry-run"
        else:
            try:
                today_log[target_id] = f"posted:{poster.post(text)}"
            except Exception as e:  # noqa: BLE001
                today_log[target_id] = f"failed:{type(e).__name__}"
                print(f"[WARN] 投稿失敗: {e}", file=sys.stderr)
        save_post_log(log)


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
        target = targets.get(row["JANコード"]) or TargetProduct(
            row["JANコード"],
            row["商品名"],
            row["商品名"],
            "laundry_liquid",
            row["JANコード"],
        )
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
        analyses.append(ProductAnalysis(snapshot, stats, judge_signal(r24.price_per_use if r24 else None, stats)))
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
    # 自動投稿も「真の安値」を出すため、デフォルトで現在価格を再取得する。
    analyses = analyses_from_latest_history() if args.no_fetch else collect_and_analyze(append_csv=False)
    schedule_posts(analyses, args.dry_run, args.start_hour, args.end_hour, args.interval)


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
    sp = sub.add_parser("schedule", help="朝7時から1時間ごとにXへ自動投稿")
    sp.add_argument("--no-fetch", action="store_true", help="現在価格を再取得せずCSV最新行から投稿文を作る")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--start-hour", type=int, default=7)
    sp.add_argument("--end-hour", type=int, default=22)
    sp.add_argument("--interval", type=int, default=60)
    sp.set_defaults(func=cmd_schedule)
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
