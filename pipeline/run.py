#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright"]
# ///
"""Replay a skill.json against a live page. Fixed interpreter, no eval.

    ./pipeline/run.py opp_01 --dry-run
    ./pipeline/run.py opp_01 --headed --param company="Acme Ltd"
    ./pipeline/run.py opp_01 --headed --yes          # demo mode, no prompts

Writes out/run.jsonl as it goes so the demo UI can tail it.
"""
import argparse, json, sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

DESTRUCTIVE = {"submit", "send", "save", "delete", "pay", "confirm", "buy", "remove"}


def locate(page, L):
    by = L.get("by")
    if by == "role":
        return page.get_by_role(L["role"], name=L.get("name"))
    if by == "label":
        return page.get_by_label(L["value"])
    if by == "placeholder":
        return page.get_by_placeholder(L["value"])
    if by == "text":
        return page.get_by_text(L["value"])
    if by == "testid":
        return page.get_by_test_id(L["value"])
    if by == "css":
        return page.locator(L["value"])
    raise ValueError(f"unknown locator: {L}")


def describe(a):
    L = a.get("locator") or {}
    what = L.get("name") or L.get("value") or a.get("url") or a.get("key") or ""
    return f"{a['op']:<11} {what}"


class Log:
    def __init__(self, path):
        self.f = Path(path).open("w")

    def __call__(self, **ev):
        ev["ts"] = round(time.time(), 3)
        self.f.write(json.dumps(ev) + "\n")
        self.f.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("skill_id")
    ap.add_argument("--skills", default="out/skills")
    ap.add_argument("--param", action="append", default=[], metavar="K=V")
    ap.add_argument("--headed", action="store_true", help="show the browser (use this on stage)")
    ap.add_argument("--slow", type=int, default=0, help="ms delay per action, 300 reads well live")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true", help="skip confirmation prompts")
    ap.add_argument("--events", default="out/run.jsonl")
    ap.add_argument("--timeout", type=int, default=8000,
                    help="ms before an action gives up. Keep it short on stage.")
    args = ap.parse_args()

    path = Path(args.skills) / args.skill_id / "skill.json"
    if not path.exists():
        sys.exit(f"No skill at {path}. Run compile.py first.")
    skill = json.loads(path.read_text())
    actions = skill.get("actions", [])

    params = {}
    for p in args.param:
        k, _, v = p.partition("=")
        params[k] = v
    missing = [p["name"] for p in skill.get("params", []) if p["name"] not in params]

    print(f"\n{skill.get('name')}  ({len(actions)} actions)")
    if args.dry_run:
        for i, a in enumerate(actions, 1):
            flag = "  [confirm]" if a.get("confirm") else ""
            src = f"  <- ${a['param']}" if a.get("param") else ""
            print(f"  {i:>2}. {describe(a)}{src}{flag}")
        if missing:
            print(f"\nNeeds params: {', '.join(missing)}")
        return

    if missing:
        sys.exit(f"Missing params: {', '.join(missing)}  (pass --param name=value)")

    log = Log(args.events)
    log(event="run_start", skill=skill.get("name"), actions=len(actions),
        human_baseline_seconds=skill.get("human_baseline_seconds"))

    extracted, failed = {}, None
    t0 = time.time()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed, slow_mo=args.slow)
        page = browser.new_page()
        page.set_default_timeout(args.timeout)
        start = skill.get("start_url")
        if start:
            if "://" not in start:  # repo-relative path, e.g. fixtures/fake-crm.html
                start = (Path(__file__).resolve().parent.parent / start).as_uri()
            page.goto(start)

        for i, a in enumerate(actions, 1):
            label = describe(a)
            name = (a.get("locator") or {}).get("name", "") or ""
            risky = a.get("confirm") or any(w in name.lower() for w in DESTRUCTIVE)

            if risky and not args.yes:
                print(f"\n  {i}. {label}")
                if input("     this one acts for real. run it? [y/N] ").strip().lower() != "y":
                    log(event="skipped", i=i, action=a)
                    print("     skipped")
                    continue

            log(event="action_start", i=i, action=a)
            s = time.time()
            try:
                op = a["op"]
                if op == "goto":
                    page.goto(a["url"])
                elif op == "fill":
                    val = params[a["param"]] if a.get("param") else a.get("value", "")
                    locate(page, a["locator"]).fill(val)
                elif op == "click":
                    locate(page, a["locator"]).click()
                elif op == "select":
                    locate(page, a["locator"]).select_option(a["value"])
                elif op == "press":
                    page.keyboard.press(a["key"])
                elif op == "wait_for":
                    locate(page, a["locator"]).wait_for(timeout=a.get("timeout_ms", 10000))
                elif op == "extract":
                    extracted[a["as"]] = locate(page, a["locator"]).inner_text()
                elif op == "assert_text":
                    got = locate(page, a["locator"]).inner_text()
                    if a["text"].lower() not in got.lower():
                        raise AssertionError(f"expected {a['text']!r}, page said {got[:80]!r}")
                else:
                    raise ValueError(f"unknown op {op}")
            except (PWTimeout, AssertionError, Exception) as e:
                failed = (i, label, f"{type(e).__name__}: {e}")
                log(event="action_failed", i=i, error=str(e))
                print(f"  {i:>2}. {label}  FAILED  {e}")
                break

            dt = time.time() - s
            log(event="action_ok", i=i, seconds=round(dt, 2))
            print(f"  {i:>2}. {label}  ok ({dt:.1f}s)")

        if args.headed:
            input("\n  browser is open. enter to close ")
        browser.close()

    total = time.time() - t0
    human = skill.get("human_baseline_seconds") or 0
    log(event="run_end", seconds=round(total, 2), ok=failed is None, extracted=extracted)

    print()
    if failed:
        print(f"  stopped at action {failed[0]} ({failed[1]})")
        print(f"  {failed[2]}")
        sys.exit(1)
    if extracted:
        for k, v in extracted.items():
            print(f"  {k} = {v[:60]}")
    print(f"  done in {total:.1f}s", end="")
    if human:
        print(f"  (human: {human}s, {human / total:.0f}x faster)")
    else:
        print()
    print(f"  events -> {args.events}")


if __name__ == "__main__":
    main()
