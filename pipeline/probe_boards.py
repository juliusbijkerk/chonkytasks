#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright"]
# ///
"""Which job sources can an agent actually read, by default, with no login?

    ./pipeline/probe_boards.py

LinkedIn and Instagram are the wrong target: login-walled, bot-walled, and
automating them is against their terms. Applicant tracking systems are the
opposite. A company's careers page is published so that strangers and crawlers
will read it. That is its entire job.

One request per host. This is a compatibility check, not a scrape.
"""
import argparse, json, re, sys
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BOARDS = [
    ("Greenhouse",  "https://job-boards.greenhouse.io/duolingo"),
    ("Greenhouse2", "https://boards.greenhouse.io/figma"),
    ("Lever",       "https://jobs.lever.co/voiceflow"),
    ("Ashby",       "https://jobs.ashbyhq.com/openai"),
    ("Workable",    "https://apply.workable.com/mistral-ai/"),
    ("Recruitee",   "https://bynder.recruitee.com/"),
    ("SmartRecruit","https://careers.smartrecruiters.com/Bosch"),
    ("Personio",    "https://ratepay.jobs.personio.de/"),
    ("Dunelm",      "https://www.dunelm.com/search?q=mattress%20protector"),  # control
]

BLOCK = re.compile(r"(access denied|are you a human|unusual traffic|verify you are|"
                   r"captcha|robot check|request blocked|just a moment)", re.I)
LOGIN = re.compile(r"(sign in to continue|log in to view|create an account to)", re.I)


def probe(page, name, url):
    r = {"source": name, "url": url}
    try:
        resp = page.goto(url, wait_until="commit", timeout=25000)
        r["status"] = resp.status if resp else None
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except PWTimeout:
            pass
        page.wait_for_timeout(3000)
    except Exception as e:
        r["status"] = "error"
        r["detail"] = str(e).split("\n")[0][:120]
        return r

    try:
        body = page.inner_text("body")[:30000]
        r["blocked"] = bool(BLOCK.search(body))
        r["login_wall"] = bool(LOGIN.search(body))
        # a job link, by any of the usual ATS shapes
        r["job_links"] = page.locator(
            "a[href*='/jobs/'], a[href*='/job/'], a[href*='/o/'], "
            "a[href*='/opportunities/'], a[href*='gh_jid'], a[href*='/p/']").count()
        heads = page.locator("h1, h2, h3")
        r["headings"] = heads.count()
        r["sample"] = " | ".join(
            " ".join(heads.nth(i).inner_text().split())[:42]
            for i in range(min(heads.count(), 3)))
    except Exception as e:
        r["detail"] = f"{type(e).__name__}: {e}"[:120]
    return r


def verdict(r):
    if r.get("status") in ("error", None) or isinstance(r.get("status"), str):
        return "NO", r.get("detail", "did not load")[:60]
    if r.get("blocked"):
        return "NO", "bot wall"
    if r.get("login_wall"):
        return "NO", "login wall"
    if r.get("status", 0) >= 400:
        return "NO", f"http {r['status']}"
    if r.get("job_links", 0) >= 3:
        return "OK", f"{r['job_links']} job links, {r.get('headings',0)} headings"
    return "WEAK", f"only {r.get('job_links',0)} job links"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out/board_probe.json")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    rows = []
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=not args.headed)
        for name, url in BOARDS:
            # fresh context per host: a previous site's pending timers fire a
            # navigation that interrupts the next goto if you reuse one page
            ctx = b.new_context(locale="en-GB", viewport={"width": 1400, "height": 900})
            page = ctx.new_page()
            r = probe(page, name, url)
            ctx.close()
            v, why = verdict(r)
            r["verdict"], r["why"] = v, why
            rows.append(r)
            print(f"  {v:<5} {name:<13} {why}")
            if r.get("sample"):
                print(f"        {r['sample'][:96]}")
        b.close()

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(rows, indent=2))
    good = [r["source"] for r in rows if r["verdict"] == "OK"]
    print(f"\n  usable: {', '.join(good) if good else 'none'}")
    print(f"  -> {args.out}")


if __name__ == "__main__":
    main()
