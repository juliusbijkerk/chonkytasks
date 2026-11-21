#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright"]
# ///
"""Can Playwright actually drive these sites? One page load each, nothing more.

    ./pipeline/probe_sites.py
    ./pipeline/probe_sites.py --headed

Reports, per site: did it load, is there a cookie wall, does it look blocked,
and can we find product names and prices. Decide the demo targets from this,
not from a hunch. One request per site: this is a compatibility check, not a
scrape, so do not turn it into a loop.
"""
import argparse, json, re, sys
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

SITES = [
    ("John Lewis", "https://www.johnlewis.com/search?search-term=mattress+protector"),
    ("Argos",      "https://www.argos.co.uk/search/mattress-protector/"),
    ("Dunelm",     "https://www.dunelm.com/search?q=mattress%20protector"),
    ("IKEA",       "https://www.ikea.com/gb/en/search/?q=mattress%20protector"),
    ("Next",       "https://www.next.co.uk/search?w=mattress%20protector"),
    ("Wayfair",    "https://www.wayfair.co.uk/keyword.php?keyword=mattress+protector"),
    ("eBay",       "https://www.ebay.co.uk/sch/i.html?_nkw=mattress+protector"),
    ("Amazon",     "https://www.amazon.co.uk/s?k=mattress+protector"),
]

BLOCK = re.compile(
    r"(access denied|are you a human|unusual traffic|verify you are|captcha|"
    r"robot check|request blocked|pardon our interruption|enable javascript and cookies)",
    re.I)
COOKIE = re.compile(r"(accept all|accept cookies|manage cookies|cookie preferences|"
                    r"we use cookies|your privacy)", re.I)
PRICE = re.compile(r"£\s?\d{1,4}(?:[.,]\d{2})?")


def probe(page, name, url):
    r = {"site": name, "url": url}
    try:
        # "commit" instead of "domcontentloaded": these sites fire a client-side
        # redirect (geo, consent) that interrupts a pending navigation otherwise.
        resp = page.goto(url, wait_until="commit", timeout=25000)
        r["status"] = resp.status if resp else None
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except PWTimeout:
            pass
    except PWTimeout:
        r["status"] = "timeout"
    except Exception as e:
        r["status"] = f"error: {type(e).__name__}"
        r["detail"] = str(e).split("\n")[0][:160]
        return r

    try:
        page.wait_for_timeout(2500)
        body = page.inner_text("body")[:20000]
        r["title"] = (page.title() or "")[:70]
        r["blocked"] = bool(BLOCK.search(body))
        r["cookie_wall"] = bool(COOKIE.search(body))
        r["prices_found"] = len(set(PRICE.findall(body)))
        r["links"] = page.locator("a[href*='/p/'], a[href*='/product'], a[href*='/itm/']").count()
    except Exception as e:
        r["error"] = f"{type(e).__name__}: {e}"
    return r


def verdict(r):
    if isinstance(r.get("status"), str) or r.get("status") is None:
        return "NO", "did not load"
    if r.get("blocked"):
        return "NO", "bot wall"
    if r.get("status", 0) >= 400:
        return "NO", f"http {r['status']}"
    if r.get("prices_found", 0) >= 3:
        return ("OK" if not r.get("cookie_wall") else "OK*"), \
               f"{r['prices_found']} prices, {r.get('links', 0)} product links"
    return "WEAK", f"only {r.get('prices_found', 0)} prices visible"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--out", default="out/site_probe.json")
    args = ap.parse_args()

    rows = []
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=not args.headed)
        for name, url in SITES:
            # Fresh context per site. A previous site's pending timers fire a
            # navigation that interrupts the next goto if you reuse one page.
            ctx = b.new_context(locale="en-GB", viewport={"width": 1400, "height": 900})
            page = ctx.new_page()
            r = probe(page, name, url)
            ctx.close()
            v, why = verdict(r)
            r["verdict"], r["why"] = v, why
            rows.append(r)
            print(f"  {v:<5} {name:<12} {why}")
        b.close()

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(rows, indent=2))
    good = [r["site"] for r in rows if r["verdict"].startswith("OK")]
    print(f"\n  usable: {', '.join(good) if good else 'none'}")
    print(f"  * = loads fine but has a cookie wall to dismiss first")
    print(f"  -> {args.out}")


if __name__ == "__main__":
    main()
