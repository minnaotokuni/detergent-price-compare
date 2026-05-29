"""OGP画像 (1200x630) を生成する開発用ビルドスクリプト。

CSV最新データから各カテゴリの単価最安を拾い、SNSシェア用のカード画像を作る。
    python _make_og.py   # → og-image.png
"""

import html
from pathlib import Path

from playwright.sync_api import sync_playwright

import main as m

BASE = Path(__file__).resolve().parent
OUT = BASE / "og-image.png"


def build_html() -> str:
    analyses = m.analyses_from_latest_history()
    chips = []
    for cat_key in ("laundry_liquid", "laundry_powder", "fabric_softener", "dish", "bath_toilet", "body_soap"):
        cands = [a for a in analyses if a.snapshot.target.category_key == cat_key and a.unit_price is not None]
        if not cands:
            continue
        best = min(cands, key=lambda a: a.unit_price or 1e9)
        ui = m.category_ui(cat_key)
        chips.append(
            f'<div style="display:flex;align-items:center;gap:10px;background:#fff;border-radius:16px;'
            f'padding:14px 18px;box-shadow:0 6px 20px rgba(0,0,0,.08)">'
            f'<span style="font-size:26px">{ui["emoji"]}</span>'
            f'<div><div style="font-size:15px;color:#64748b;font-weight:700">{html.escape(ui["label"])}</div>'
            f'<div style="font-size:24px;font-weight:900;color:#0f172a">¥{best.unit_price:.1f}'
            f'<span style="font-size:14px;color:#94a3b8"> /{best.unit_basis}</span></div></div></div>'
        )
    grid = "".join(chips[:6])
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;700;900&display=swap" rel="stylesheet">
<style>*{{margin:0;box-sizing:border-box;font-family:'Noto Sans JP',sans-serif}}</style></head>
<body style="width:1200px;height:630px;overflow:hidden;
background:linear-gradient(135deg,#e11d48,#ef4444 55%,#f97316);color:#fff;padding:64px;position:relative">
<div style="position:absolute;top:-80px;right:-80px;width:340px;height:340px;border-radius:50%;background:rgba(255,255,255,.12)"></div>
<div style="font-size:22px;font-weight:900;letter-spacing:.2em;opacity:.85">単位価格で選ぶ 洗剤・日用品比較</div>
<div style="font-size:62px;font-weight:900;line-height:1.15;margin-top:14px">内容量あたりの単価で、<br>本当に割安な<span style="text-decoration:underline;text-decoration-color:#fcd34d;text-decoration-thickness:6px">1本</span>を見つける。</div>
<div style="font-size:24px;opacity:.92;margin-top:18px">円/100g・円/100ml で客観比較 ｜ 価格推移・買い時もチェック</div>
<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-top:40px">{grid}</div>
</body></html>"""


def main_run() -> None:
    page_html = build_html()
    tmp = BASE / "_og_tmp.html"
    tmp.write_text(page_html, encoding="utf-8")
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1200, "height": 630}, device_scale_factor=1)
        pg.goto(tmp.as_uri(), wait_until="networkidle")
        pg.wait_for_timeout(800)
        pg.screenshot(path=str(OUT), clip={"x": 0, "y": 0, "width": 1200, "height": 630})
        b.close()
    tmp.unlink(missing_ok=True)
    print(f"saved {OUT}")


if __name__ == "__main__":
    main_run()
