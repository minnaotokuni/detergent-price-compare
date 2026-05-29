"""開発用スクリーンショット撮影ヘルパー (本番デプロイには含めない)。

使い方:
    python _shot.py index.html shots/after.png            # フルページ
    python _shot.py index.html shots/mobile.png --mobile  # モバイル幅
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = Path(__file__).resolve().parent


def main() -> None:
    src = BASE / (sys.argv[1] if len(sys.argv) > 1 else "index.html")
    out = BASE / (sys.argv[2] if len(sys.argv) > 2 else "shots/site.png")
    mobile = "--mobile" in sys.argv
    out.parent.mkdir(parents=True, exist_ok=True)
    width = 390 if mobile else 1280
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 900}, device_scale_factor=2)
        page.goto(src.as_uri(), wait_until="networkidle")
        page.wait_for_timeout(900)
        page.screenshot(path=str(out), full_page=True)
        browser.close()
    print(f"saved {out}")


if __name__ == "__main__":
    main()
