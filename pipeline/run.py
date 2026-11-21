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
import argparse, json, re, sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

DESTRUCTIVE = {"submit", "send", "save", "delete", "pay", "confirm", "buy", "remove"}

TPL = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def fill_template(text, values):
    """Substitute {{name}} from params and anything extracted so far.

    The compiler emits these all over: start_url is "{{job_url}}", an
    assert_text checks "{{job_title}}" against a heading it extracted two
    steps earlier. Without this the runner navigated to the literal string
    "{{job_url}}" and asserted on literal braces, which fails in a way that
    looks like the page is wrong rather than the runner.
    """
    if not isinstance(text, str):
        return text
    missing = []

    def sub(m):
        k = m.group(1)
        if k in values and values[k] is not None:
            return str(values[k])
        missing.append(k)
        return m.group(0)

    out = TPL.sub(sub, text)
    if missing:
        raise KeyError("nothing to fill " + ", ".join("{{%s}}" % k for k in missing))
    return out


def locate(page, L, values=None):
    """Resolve a locator, filling {{name}} inside it first.

    Locators carry templates as often as urls do: the compiler emits
    {"by": "text", "value": "{{job_title}}"} because the title is the thing
    that changes between passes. Without filling them here the runner hunted
    the live page for the literal string "{{job_title}}" and timed out, which
    reads exactly like the page having changed shape.
    """
    v = values or {}
    by = L.get("by")
    name = fill_template(L.get("name"), v) if L.get("name") is not None else None
    val = fill_template(L.get("value"), v) if L.get("value") is not None else None
    # "exact": true when the name is a prefix of a longer one on the same page.
    # Ashby's board carries both a "Location" and a "Location Type" filter, and
    # Playwright's default substring match hits both, which fails as a strict
    # mode violation rather than as a missing element. Ambiguity SHOULD fail
    # loudly; this is the way to say which one you meant.
    exact = bool(L.get("exact"))
    if by == "role":
        return page.get_by_role(L["role"], name=name, exact=exact)
    if by == "label":
        return page.get_by_label(val, exact=exact)
    if by == "placeholder":
        return page.get_by_placeholder(val, exact=exact)
    if by == "text":
        return page.get_by_text(val, exact=exact)
    if by == "testid":
        return page.get_by_test_id(val)
    if by == "css":
        return page.locator(val)
    raise ValueError(f"unknown locator: {L}")


def select_option(loc, want):
    """Choose an option by label, tolerating a count suffix.

    Job boards label their filters with the tally: Ashby's Location list reads
    "London (88)", not "London". Playwright matches option labels exactly, so
    an honest "London" from the recording found nothing and the runner spent
    30 seconds retrying an element it had already located. Try the exact match
    first, then the option whose text starts with what was asked for.
    """
    opts = loc.locator("option")
    for i in range(opts.count()):
        o = opts.nth(i)
        text = " ".join((o.inner_text() or "").split())
        if text.lower().startswith(want.lower()):
            loc.select_option(o.get_attribute("value"))
            return
    # no <option> children, so it is a custom widget: let Playwright try
    loc.select_option(want, timeout=2500)


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
    values = dict(params)
    t0 = time.time()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed, slow_mo=args.slow)
        page = browser.new_page()
        page.set_default_timeout(args.timeout)
        start = skill.get("start_url")
        if start:
            start = fill_template(start, values)   # start_url is often "{{job_url}}"
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
                    page.goto(fill_template(a["url"], values))
                elif op == "fill":
                    val = (params[a["param"]] if a.get("param")
                           else fill_template(a.get("value", ""), values))
                    locate(page, a["locator"], values).fill(val)
                elif op == "click":
                    locate(page, a["locator"], values).click()
                elif op == "select":
                    select_option(locate(page, a["locator"], values),
                                  fill_template(a["value"], values))
                elif op == "press":
                    page.keyboard.press(a["key"])
                elif op == "wait_for":
                    locate(page, a["locator"], values).wait_for(timeout=a.get("timeout_ms", 10000))
                elif op == "extract":
                    got = locate(page, a["locator"], values).inner_text().strip()
                    extracted[a["as"]] = got
                    values[a["as"]] = got
                elif op == "assert_text":
                    want = fill_template(a["text"], values)
                    got = locate(page, a["locator"], values).inner_text()
                    if want.lower() not in got.lower():
                        raise AssertionError(f"expected {want!r}, page said {got[:80]!r}")
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
