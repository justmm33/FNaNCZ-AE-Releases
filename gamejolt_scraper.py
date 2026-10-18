#!/usr/bin/env python3
import argparse
import csv
import json
import re
import sys
import time
import urllib.parse

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("Playwright is missing. Run: pip install playwright && playwright install chromium")

# label written to the csv -> Game Jolt tag(s) to pull from
TAGS = {
    "undertale": ["undertale"],
    "bendy": ["bendy"],
    "fnaf": ["fnaf"],
    "fnf": ["fnf"],
    "sonic": ["sonic"],
}

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
BASE = "https://gamejolt.com"
# browse pages tried per tag until one produces a list of games
PAGE_URLS = ["/games/{sort}/tag-{tag}", "/games/tag-{tag}/{sort}", "/games/tag-{tag}"]
DELAY = 0.6      # seconds between page requests
MAX_EMPTY = 3    # stop a tag after this many pages in a row with nothing new


def find_games(obj, out=None):
    """Walks Game Jolt's JSON and returns [(id, slug, title)] for every game-looking object, in order."""
    if out is None:
        out = []
    if isinstance(obj, dict):
        gid, slug, title = obj.get("id"), obj.get("slug"), obj.get("title")
        if isinstance(gid, int) and isinstance(slug, str) and isinstance(title, str) and slug:
            out.append((gid, slug, title))
            return out
        for v in obj.values():
            find_games(v, out)
    elif isinstance(obj, list):
        for v in obj:
            find_games(v, out)
    return out


def with_page(url, n):
    """Same api url but with ?page=n."""
    parts = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    q["page"] = [str(n)]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q, doseq=True)))


FETCH_JS = """async (u) => {
    try { const r = await fetch(u, {credentials: 'include'}); if (!r.ok) return null; return await r.json(); }
    catch (e) { return null; }
}"""


def scrape_tag(ctx, label, site_tags, sort, count, debug):
    """Most popular `count` games for one label -> ordered {id: (title, slug)}."""
    found = {}

    def add(games):
        new = 0
        for gid, slug, title in games:
            if gid not in found and len(found) < count:
                found[gid] = (title, slug)
                new += 1
        print(f"  {label}: {len(found)}/{count}", end="\r", flush=True)
        return new

    for site_tag in site_tags:
        if len(found) >= count:
            break
        page = ctx.new_page()
        captured = []   # (api_url, games) for every site-api json response that holds a game list

        def on_response(resp):
            try:
                if "site-api" in resp.url and resp.request.resource_type in ("xhr", "fetch"):
                    games = find_games(resp.json())
                    if len(games) >= 3:
                        captured.append((resp.url, games))
            except Exception:
                pass

        page.on("response", on_response)

        api_url = None
        for pat in PAGE_URLS:
            captured.clear()
            url = BASE + pat.format(sort=sort, tag=urllib.parse.quote(site_tag))
            if debug:
                print(f"\n  [debug] opening {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                if debug:
                    print(f"  [debug] goto failed: {e}")
                continue
            for _ in range(60):     # up to ~15s for the game list to arrive
                if captured:
                    break
                page.wait_for_timeout(250)
            if captured:
                api_url, first = max(captured, key=lambda c: len(c[1]))
                add(first)
                if debug:
                    print(f"  [debug] game list api: {api_url} ({len(first)} games on first page)")
                break

        if not api_url:
            print(f"\n  {label}: couldn't find a game list for tag '{site_tag}'"
                  + ("" if debug else " (run with --debug --show to see why)"))
            page.close()
            continue

        # 1) fast path: replay the same api call with ?page=2,3,...
        n, empty = 2, 0
        while len(found) < count and empty < MAX_EMPTY:
            data = page.evaluate(FETCH_JS, with_page(api_url, n))
            games = find_games(data) if data else []
            if not games:
                break
            empty = 0 if add(games) else empty + 1
            n += 1
            time.sleep(DELAY)

        # 2) fallback: the page may load more as you scroll / click "load more"
        if len(found) < count:
            idle = 0
            seen_caps = len(captured)
            while len(found) < count and idle < 5:
                page.mouse.wheel(0, 20000)
                try:
                    btn = page.get_by_role("button", name=re.compile(r"load more|show more", re.IGNORECASE))
                    if btn.count():
                        btn.first.click(timeout=1500)
                except Exception:
                    pass
                page.wait_for_timeout(1200)
                new = 0
                for _u, games in captured[seen_caps:]:
                    new += add(games)
                seen_caps = len(captured)
                idle = 0 if new else idle + 1

        page.close()

    print(f"  {label}: {len(found)} games" + " " * 30)
    return found


def main():
    ap = argparse.ArgumentParser(description="Scrape the most popular Game Jolt games per tag into fan_games.csv")
    ap.add_argument("--count", type=int, default=800, help="games per tag (default 800)")
    ap.add_argument("--sort", choices=["best", "hot", "new"], default="best", help="Game Jolt sort (default best = most popular)")
    ap.add_argument("--out", default="fan_games.csv", help="output file (default fan_games.csv)")
    ap.add_argument("--show", action="store_true", help="show the browser window instead of running headless")
    ap.add_argument("--debug", action="store_true", help="print which pages/api calls are used")
    args = ap.parse_args()

    merged = {}     # id -> {"name", "slug", "tags"} (insertion order = tag order, then popularity)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.show)
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
        ctx.route("**/*", lambda r: r.abort() if r.request.resource_type in ("image", "font", "media") else r.continue_())
        for label, site_tags in TAGS.items():
            print(f"Scraping '{label}' ({args.sort}) ...")
            for gid, (title, slug) in scrape_tag(ctx, label, site_tags, args.sort, args.count, args.debug).items():
                entry = merged.setdefault(gid, {"name": title, "slug": slug, "tags": []})
                if label not in entry["tags"]:
                    entry["tags"].append(label)
        browser.close()

    if not merged:
        sys.exit("\nNothing was scraped. Re-run with: python gamejolt_scraper.py --show --debug")

    with open(args.out, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "link", "tags"])
        for gid, e in merged.items():
            w.writerow([e["name"], f"https://gamejolt.com/games/{e['slug']}/{gid}", ",".join(e["tags"])])
    print(f"\nWrote {len(merged)} games to {args.out}")


if __name__ == "__main__":
    main()
