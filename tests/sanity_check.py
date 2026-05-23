"""Standalone sanity check — exercises the browser layer end-to-end.

NOT a pytest test: this hits the live network and is only meant to be run
manually after install to verify Playwright works against the real Chromium.
Run with:

    python tests/sanity_check.py
"""

from __future__ import annotations

import asyncio
import sys

# Force UTF-8 on Windows consoles that default to cp1252.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from penumbra.privacy.fingerprint import FingerprintEngine
from penumbra.research.browser import PrivateBrowser
from penumbra.research.extractor import ContentExtractor


async def main() -> int:
    print("[1/3] Generating fingerprint...")
    engine = FingerprintEngine(seed=42)
    fp = engine.generate()
    print(f"  Fingerprint: {fp.user_agent[:80]}...")
    print(f"  Viewport: {fp.viewport}  Locale: {fp.locale}  TZ: {fp.timezone}")

    print("\n[2/3] Launching headless Chromium...")
    async with PrivateBrowser(fingerprint_engine=engine) as browser:
        print("  [OK] Browser launched")
        print("\n[3/3] Fetching https://example.com ...")
        page = await browser.fetch("https://example.com")
        print(f"  [OK] Status: {page.status}")
        print(f"  [OK] HTML bytes: {len(page.html)}")
        print(f"  [OK] Final URL: {page.final_url}")

        extracted = ContentExtractor().extract(page)
        if extracted is None:
            print("  [WARN] Extraction produced nothing (acceptable for example.com)")
        else:
            print(f"  [OK] Title: {extracted.title}")
            print(f"  [OK] Words extracted: {extracted.word_count}")

    print("\n[OK] All sanity checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
