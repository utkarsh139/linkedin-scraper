from __future__ import annotations
import asyncio, os, json, re
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(HERE, "li_profile")
DBG = os.path.join(HERE, "out", "debug")
KEYWORD = "AI Engineer"

URL = ("https://www.linkedin.com/search/results/content/?keywords="
       + quote(KEYWORD)
       + "&datePosted=%22past-24h%22&sortBy=%22date_posted%22")

# selectors we want to test against the live DOM
CANDIDATE_SELECTORS = [
    "div.feed-shared-update-v2",
    "div.update-components-actor",
    "[data-urn]",
    "[data-chameleon-result-urn]",
    "li.reusable-search__result-container",
    "div.search-results-container",
    "div.scaffold-finite-scroll__content",
    "main [componentkey]",
    ".artdeco-card",
]


async def main():
    from playwright.async_api import async_playwright
    os.makedirs(DBG, exist_ok=True)
    captured = []

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            PROFILE_DIR, headless=False,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        big_dir = os.path.join(DBG, "bodies")
        os.makedirs(big_dir, exist_ok=True)
        idx = {"n": 0}

        async def on_response(resp):
            u = resp.url
            if "/voyager/api/" not in u and "/graphql" not in u:
                return
            rec = {"url": u, "status": resp.status, "len": 0}
            try:
                body = await resp.text()
                rec["len"] = len(body)
                # the real search response will mention these search-cluster markers
                markers = ["SearchClusterViewModel", "searchDashClusters", "feedUpdate",
                           "EntityResultViewModel", "FeedUpdateV2", "commentary"]
                hit = next((m for m in markers if m in body), None)
                if hit:
                    rec["marker"] = hit
                    rec["has_posts"] = True
                # dump any sizeable body for offline inspection
                if rec["len"] > 5000:
                    idx["n"] += 1
                    fn = f"{idx['n']:02d}_{rec['len']}.json"
                    with open(os.path.join(big_dir, fn), "w", encoding="utf-8") as f:
                        f.write(body)
                    rec["saved"] = fn
            except Exception as e:
                rec["err"] = str(e)
            captured.append(rec)

        page.on("response", on_response)

        await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
        if "login" in page.url or "checkpoint" in page.url:
            print("Not logged in. Log in, then press Enter...")
            await asyncio.get_event_loop().run_in_executor(None, input, "")

        print(f"Navigating to search: {URL}")
        await page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        # scroll a few times to trigger lazy load
        for _ in range(6):
            await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2500)

        print("\nFinal URL:", page.url)

        # 0) in-browser probe: detect cards by "Feed post" innerText prefix, dump FULL text
        probe = await page.evaluate(r"""
        () => {
          const isCard = el => {
            const t = el.innerText || '';
            if (!t.startsWith('Feed post')) return false;
            return (t.match(/Feed post/g) || []).length === 1;   // exactly one post
          };
          const all = [...document.querySelectorAll('main *')].filter(isCard);
          // keep only the OUTERMOST single-post element (the full card)
          const cards = all.filter(el => !all.some(o => o !== el && o.contains(el)));
          const dump = cards.map(el => {
            const links = [...el.querySelectorAll('a[href]')].map(a => a.href);
            const profile = links.find(h => /\/in\/|\/company\//.test(h)) || '';
            const imgAlt = (el.querySelector('img[alt]') || {}).alt || '';
            return {
              componentkey: el.getAttribute('componentkey') || '',
              profile, imgAlt,
              fullText: (el.innerText || ''),
            };
          });
          return { nCards: cards.length, dump: dump.slice(0, 4) };
        }
        """)
        print("\n=== in-browser probe: nCards =", probe["nCards"], "===")
        for i, c in enumerate(probe["dump"]):
            print(f"\n----- CARD {i} | componentkey={c['componentkey']!r} -----")
            print("profile:", c["profile"])
            print("imgAlt :", c["imgAlt"])
            print("FULLTEXT:")
            print(c["fullText"])
        with open(os.path.join(DBG, "probe.json"), "w", encoding="utf-8") as f:
            json.dump(probe, f, ensure_ascii=False, indent=2)

        # 1) selector counts
        print("\n=== selector counts ===")
        sel_counts = {}
        for s in CANDIDATE_SELECTORS:
            try:
                c = await page.evaluate("(s)=>document.querySelectorAll(s).length", s)
            except Exception as e:
                c = f"err:{e}"
            sel_counts[s] = c
            print(f"  {c!s:>6}  {s}")

        # 2) any urns present in the page
        html = await page.content()
        urns = sorted(set(re.findall(r"urn:li:(?:activity|ugcPost|share):\d+", html)))
        print(f"\nactivity urns in page HTML: {len(urns)}")
        print("  sample:", urns[:5])

        # 3) captured voyager calls
        print(f"\n=== captured /voyager/api/ responses: {len(captured)} ===")
        for c in captured:
            tag = " <<< HAS POST CONTENT" if c.get("has_posts") else ""
            short = c["url"].split("/voyager/api/")[-1][:90]
            print(f"  {c['status']} len={c['len']:>7}  {short}{tag}")

        # dump everything to disk
        with open(os.path.join(DBG, "page.html"), "w", encoding="utf-8") as f:
            f.write(html)
        with open(os.path.join(DBG, "captured.json"), "w", encoding="utf-8") as f:
            json.dump(captured, f, ensure_ascii=False, indent=2)
        with open(os.path.join(DBG, "selectors.json"), "w", encoding="utf-8") as f:
            json.dump({"url": page.url, "sel_counts": sel_counts,
                       "urn_count": len(urns), "urns": urns[:20]}, f, indent=2)
        print(f"\nWrote: {DBG}\\page.html, captured.json, selectors.json")
        print("Leaving browser open 8s for inspection...")
        await page.wait_for_timeout(8000)
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
