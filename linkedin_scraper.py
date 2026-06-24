"""
LinkedIn Content Search scraper — simple utility.

Searches LinkedIn Content Search for one or many keywords, filters to
Posts from the last 24 hours, scrolls to load all visible results, and
exports everything to posts.xlsx + posts.csv.

Strategy:
  1. Persistent browser profile  -> log in once, reused forever.
  2. Intercept Voyager/GraphQL responses (reliable) for each keyword.
  3. Fall back to DOM scraping for anything the API parse misses.
  4. Merge by post activity id, dedupe, export.

Usage:
  python linkedin_scraper.py --keyword "AI Engineer"
  python linkedin_scraper.py --keyword-file keywords.txt
  python linkedin_scraper.py                       # uses built-in default keywords
  python linkedin_scraper.py --headless            # after you've logged in once

First run: a Chrome window opens. Log in to LinkedIn manually (handles 2FA),
then press Enter in the terminal. The session is saved in ./li_profile.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote

import pandas as pd

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(HERE, "li_profile")     # persistent login lives here
DEFAULT_OUT = os.path.join(HERE, "out")
MAX_SCROLLS = 40
SCROLL_STABLE_ROUNDS = 4          # stop after this many scrolls with no new posts
SCROLL_MIN_MS, SCROLL_MAX_MS = 1600, 2900

ACT_RE = re.compile(r"urn:li:(?:activity|ugcPost|share):\d+")

DEFAULT_KEYWORDS = [
    # Software Engineering
    "Software Engineer", "Software Developer", "Full Stack Developer",
    "Fullstack Developer", "Full Stack Engineer", "Backend Developer",
    "Backend Engineer", "Frontend Developer", "Frontend Engineer",
    "Web Developer", "Application Developer", "Python Developer",
    "Java Developer", "Node.js Developer", "React Developer",
    "MERN Developer", "SDE", "SDE 1", "SDE 2",
    # AI / GenAI
    "AI Engineer", "AI Developer", "AI Software Engineer",
    "Generative AI Engineer", "GenAI Engineer", "LLM Engineer",
    "LLM Developer", "Agentic AI Engineer", "AI Automation Engineer",
    "AI Product Engineer", "AI Research Engineer", "Applied AI Engineer",
    "AI Solutions Engineer", "AI Architect",
    # Machine Learning
    "Machine Learning Engineer", "ML Engineer",
    "Applied Machine Learning Engineer", "Deep Learning Engineer",
    "Computer Vision Engineer", "NLP Engineer", "MLOps Engineer",
    "AI ML Engineer", "ML Research Engineer",
    # Data Science
    "Data Scientist", "Senior Data Scientist", "Applied Scientist",
    "Data Analyst", "Analytics Engineer", "Business Intelligence Engineer",
    "Data Engineer", "Big Data Engineer", "Data Platform Engineer",
    "Decision Scientist",
    # Cloud / Platform
    "Platform Engineer", "DevOps Engineer", "Cloud Engineer",
    "Site Reliability Engineer", "Infrastructure Engineer",
    # Hiring intent
    "Hiring AI Engineer", "Hiring Machine Learning Engineer",
    "Hiring Data Scientist", "Hiring Software Engineer",
    "Hiring Full Stack Developer", "Looking for Software Engineer",
    "Looking for AI Engineer", "Looking for ML Engineer",
    "Looking for Data Scientist", "Looking for Full Stack Developer",
    "We Are Hiring AI Engineer", "We Are Hiring Software Engineer",
    "We Are Hiring ML Engineer", "We Are Hiring Data Scientist",
    "Remote Software Engineer", "Remote AI Engineer", "Remote ML Engineer",
    "Remote Data Scientist", "Entry Level Software Engineer",
    "Entry Level AI Engineer", "Fresher Software Engineer",
    "Fresher Developer", "Graduate Software Engineer", "Graduate AI Engineer",
    "Software Engineer Internship", "Software Developer Internship",
    "AI Internship", "ML Internship", "Data Science Internship",
    "Generative AI Internship",
]

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def activity_id(s: str) -> str:
    m = ACT_RE.search(s or "")
    return m.group(0) if m else ""


def parse_count(text) -> int:
    """'1,234' / '1.2K' / '12 comments' / '3 reposts' -> int."""
    if text is None:
        return 0
    if isinstance(text, (int, float)):
        return int(text)
    m = re.search(r"([\d][\d.,]*)\s*([KkMm]?)", str(text))
    if not m:
        return 0
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return 0
    suf = m.group(2).lower()
    if suf == "k":
        num *= 1_000
    elif suf == "m":
        num *= 1_000_000
    return int(num)


def build_search_url(keyword: str) -> str:
    # Content vertical = Posts. datePosted "past-24h", sorted latest first.
    return ("https://www.linkedin.com/search/results/content/?keywords="
            + quote(keyword)
            + "&datePosted=%22past-24h%22&sortBy=%22date_posted%22")


def dedup_key(text: str) -> str:
    """Posts in this LinkedIn build have no activity URN, so we dedupe on the
    normalized post text (same post under multiple keywords collapses)."""
    return re.sub(r"\s+", " ", (text or "")).strip().lower()[:200]


def author_from_alt(alt: str) -> str:
    """'View Vikram Bharwada’s profile' / 'View company: Codex ...' -> name."""
    alt = (alt or "").strip()
    m = re.match(r"^View company:\s*(.+)$", alt)
    if m:
        return m.group(1).strip()
    m = re.match(r"^View\s+(.+?)[’'`]s profile$", alt)
    if m:
        return m.group(1).strip()
    return alt


def resolve_keywords(args) -> list[str]:
    if args.keyword:
        return [args.keyword]
    if args.keyword_file:
        with open(args.keyword_file, encoding="utf-8") as f:
            kws = [ln.strip() for ln in f
                   if ln.strip() and not ln.strip().startswith("#")]
        if not kws:
            sys.exit(f"No keywords found in {args.keyword_file}")
        return kws
    return DEFAULT_KEYWORDS


# ---------------------------------------------------------------------------
# DOM scraping
# ---------------------------------------------------------------------------
#
# LinkedIn's current search build renders posts as web-components with hashed,
# per-build CSS class names and NO data-urn / activity-id attributes, so the
# old fixed-selector and Voyager-JSON strategies no longer work. The only
# stable anchor is the rendered innerText: every result card's text begins with
# "Feed post". We isolate each card by that prefix, then parse its text.

# Returns one object per card: {profile, imgAlt, text}. Card = the OUTERMOST
# element whose innerText starts with "Feed post" and contains exactly one post.
DOM_EXTRACT_JS = r"""
() => {
  const isCard = (el) => {
    const t = el.innerText || "";
    if (!t.startsWith("Feed post")) return false;
    return (t.match(/Feed post/g) || []).length === 1;
  };
  const all = [...document.querySelectorAll("main *")].filter(isCard);
  const cards = all.filter((el) => !all.some((o) => o !== el && o.contains(el)));
  return cards.map((el) => {
    const links = [...el.querySelectorAll("a[href]")].map((a) => a.href);
    const profile = links.find((h) => /\/in\/|\/company\//.test(h)) || "";
    const img = el.querySelector("img[alt]");
    return { profile, imgAlt: img ? img.alt : "", text: el.innerText || "" };
  });
}
"""

# click any "…more" / "see more" inline-expanders so we capture full post text
EXPAND_JS = r"""
() => {
  let n = 0;
  for (const b of document.querySelectorAll("main button, main span[role='button']")) {
    const t = (b.innerText || "").trim().toLowerCase().replace(/\s+/g, " ");
    if (t === "…more" || t === "… more" || t === "...more" || t === "see more") {
      try { b.click(); n++; } catch (e) {}
    }
  }
  return n;
}
"""

# counts cards cheaply during scrolling (one innerText read, not per-element)
COUNT_JS = r"""
() => {
  const m = document.querySelector("main");
  return m ? ((m.innerText || "").match(/Feed post/g) || []).length : 0;
}
"""

# --- job-post detection -----------------------------------------------------
# LinkedIn search has no "job post" content filter, so we classify on the text.
# A post is a job post if it hits one STRONG signal, or two+ MEDIUM signals.
STRONG_JOB_RE = [re.compile(p, re.I) for p in (
    r"\b(we(?:'| a)re|now|currently|actively)\s+hiring\b",
    r"\bis\s+hiring\b",
    r"\bjob\s+(?:title|description|opening|opportunit)",
    r"\bopen\s+(?:position|role|vacanc)",
    r"\b(?:share|send|drop|email|dm)\s+(?:your|me\s+your|us\s+your)?\s*(?:resume|cv|cvs)\b",
    r"\bapply\s+(?:now|here|at|via|through|by)\b",
    r"#hiring\b", r"#nowhiring\b", r"#jobopening", r"#wearehiring",
    r"\bhiring\s+(?:for|alert|now)\b",
)]
MEDIUM_JOB_RE = [re.compile(p, re.I) for p in (
    r"\b(?:looking|searching)\s+for\s+(?:an?\s+)?\w+\s+(?:engineer|developer|scientist|"
    r"analyst|manager|designer|architect|intern|specialist|lead)\b",
    r"\bjob\b", r"\bvacanc", r"\bopen\s+role", r"\bopen\s+position", r"\bopenings?\b",
    r"\bapply\b", r"\brecruit", r"\bcandidate", r"\bapplicant",
    r"\bexperience\s*[:\-]", r"\blocation\s*[:\-]", r"\bemployment\s+type\b",
    r"\bnotice\s+period\b", r"\bimmediate\s+joiner", r"\bctc\b", r"\bsalary\b",
    r"\bfull[\-\s]?time\b", r"\bpart[\-\s]?time\b", r"\bremote\b", r"\bonsite\b",
    r"\bjoin\s+(?:our|the)\s+team\b", r"\broles?\s+and\s+responsibilit",
    r"\brequired\s+skills?\b", r"\bqualificat", r"\byears?\s+of\s+experience\b",
    r"#job", r"#career", r"#vacancy", r"#opentowork", r"#recruit",
)]


def is_job_post(text: str) -> bool:
    t = text or ""
    if any(r.search(t) for r in STRONG_JOB_RE):
        return True
    return sum(1 for r in MEDIUM_JOB_RE if r.search(t)) >= 2


TIME_RE = re.compile(r"^(now|\d+\s*(?:s|m|h|d|w|mo|yr))\b", re.I)
ACTION_LINES = {"like", "comment", "repost", "send", "save",
                "follow", "following", "connect", "+ follow"}
TRAIL_NOISE = {"… more", "…more", "...more", "see more"}


def parse_card(card: dict, keyword: str) -> dict | None:
    """Turn one raw card ({profile, imgAlt, text}) into an export record."""
    text = card.get("text", "") or ""
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]
    if not lines or lines[0] != "Feed post":
        return None

    # locate the relative-time line ("now • ", "4m • Edited • ")
    time_idx = post_date = None
    for i in range(1, len(lines)):
        if "•" in lines[i] and TIME_RE.match(lines[i]):
            time_idx = i
            post_date = lines[i].split("•")[0].strip()
            break

    # actor block (between name and time) -> headline
    name = author_from_alt(card.get("imgAlt", ""))
    headline = ""
    if time_idx is not None:
        actor = []
        for ln in lines[1:time_idx]:
            low = ln.lower()
            if ln == name or ln.startswith("•") or low.startswith("view ") \
                    or low in ("verified", "premium", "open to work"):
                continue
            actor.append(ln)
        headline = " ".join(actor).strip()
    if not name and len(lines) > 1:
        name = lines[1]

    # post body: after the first action line at/after the time, up to "Like"
    body_start = (time_idx + 1) if time_idx is not None else 1
    for i in range(body_start, len(lines)):
        if lines[i].lower() in ("follow", "following", "connect", "+ follow"):
            body_start = i + 1
            break
    like_idx = next((i for i in range(body_start, len(lines))
                     if lines[i].lower() == "like"), len(lines))
    body = [ln for ln in lines[body_start:like_idx]
            if ln.lower() not in TRAIL_NOISE and ln not in TRAIL_NOISE]
    while body and (body[-1].lower() in ACTION_LINES or body[-1].lower() in TRAIL_NOISE):
        body.pop()
    post_text = "\n".join(body).strip()
    if not post_text:
        return None

    # engagement counts are usually absent in search view -> best-effort, default 0
    cm = re.search(r"([\d.,]+[KkMm]?)\s*comment", text, re.I)
    rm = re.search(r"([\d.,]+[KkMm]?)\s*repost", text, re.I)
    comments = parse_count(cm.group(1)) if cm else 0
    reposts = parse_count(rm.group(1)) if rm else 0
    reactions = 0
    if like_idx > 0 and lines[like_idx - 1].replace(",", "").replace(".", "").isdigit():
        reactions = parse_count(lines[like_idx - 1])

    return {
        "Search Keyword": keyword,
        "Author Name": name,
        "Author Headline": headline,
        "Post Text": post_text,
        "Author Profile URL": card.get("profile", ""),
        "Post Date": post_date or "",
        "Is Job Post": "Yes" if is_job_post(post_text) else "No",
        "Reactions Count": reactions,
        "Comments Count": comments,
        "Reposts Count": reposts,
        "Scraped At": now_iso(),
    }


async def extract_dom(page, keyword: str, jobs_only: bool = False) -> dict:
    """Returns {dedup_key: record} scraped from the rendered page."""
    try:
        raw = await page.evaluate(DOM_EXTRACT_JS)
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for card in raw or []:
        rec = parse_card(card, keyword)
        if not rec:
            continue
        if jobs_only and rec["Is Job Post"] != "Yes":
            continue
        out[dedup_key(rec["Post Text"])] = rec
    return out


# ---------------------------------------------------------------------------
# Browser driving
# ---------------------------------------------------------------------------

async def goto_retry(page, url: str, retries: int = 2) -> bool:
    for attempt in range(retries + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            return True
        except Exception as e:
            if attempt < retries:
                await page.wait_for_timeout(2000 * (attempt + 1))
            else:
                print(f"   ! navigation failed: {e}")
    return False


async def ensure_login(page):
    await goto_retry(page, "https://www.linkedin.com/feed/")
    await page.wait_for_timeout(3000)
    logged_out = ("login" in page.url or "checkpoint" in page.url
                  or await page.query_selector("input#username") is not None)
    if logged_out:
        print("\n" + "=" * 64)
        print(" Please log in to LinkedIn in the browser window that opened.")
        print(" Complete any 2FA / verification until you see your feed.")
        print("=" * 64)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, input, " Then press Enter here to continue... ")


async def scroll_and_collect(page, keyword: str, jobs_only: bool = False) -> dict:
    if not await goto_retry(page, build_search_url(keyword)):
        return {}
    await page.wait_for_timeout(3000)

    last_count, stable, scrolls = 0, 0, 0
    while scrolls < MAX_SCROLLS:
        await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
        # randomized human-ish pause
        pause = SCROLL_MIN_MS + int((SCROLL_MAX_MS - SCROLL_MIN_MS) * ((scrolls * 37) % 100) / 100)
        await page.wait_for_timeout(pause)
        try:
            count = await page.evaluate(COUNT_JS)
        except Exception:
            count = last_count
        if count <= last_count:
            stable += 1
            if stable >= SCROLL_STABLE_ROUNDS:
                break
        else:
            stable = 0
            last_count = count
        scrolls += 1

    # expand truncated posts so we capture full text, then extract
    try:
        await page.evaluate(EXPAND_JS)
        await page.wait_for_timeout(800)
    except Exception:
        pass
    return await extract_dom(page, keyword, jobs_only)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run(args):
    from playwright.async_api import async_playwright

    keywords = resolve_keywords(args)
    os.makedirs(args.out, exist_ok=True)
    jobs_only = args.content_type == "jobs"

    print(f"Keywords: {len(keywords)} | profile: {PROFILE_DIR} | "
          f"headless: {args.headless} | content: {args.content_type} | "
          f"max scrolls: {MAX_SCROLLS}")

    all_posts: dict[str, dict] = {}     # keyed by post-text hash, deduped across keywords

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=args.headless,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        await ensure_login(page)

        for i, kw in enumerate(keywords, 1):
            t0 = time.time()
            try:
                found = await scroll_and_collect(page, kw, jobs_only)
            except Exception as e:
                print(f"[{i}/{len(keywords)}] {kw[:34]:<34} ERROR {type(e).__name__}: {e}")
                continue

            new = 0
            for key, rec in found.items():
                if key not in all_posts:        # first keyword that found it wins
                    all_posts[key] = rec
                    new += 1
            print(f"[{i}/{len(keywords)}] {kw[:34]:<34} "
                  f"{len(found):>4} found  {new:>4} new  "
                  f"({len(all_posts)} total, {time.time()-t0:.0f}s)")

        await ctx.close()

    export(all_posts, args.out)


def export(all_posts: dict, out_dir: str):
    if not all_posts:
        print("\nNo posts collected. Nothing to export.")
        return
    cols = ["Search Keyword", "Author Name", "Author Headline", "Post Text",
            "Author Profile URL", "Post Date", "Is Job Post", "Reactions Count",
            "Comments Count", "Reposts Count", "Scraped At"]
    df = pd.DataFrame(list(all_posts.values()), columns=cols)
    df.drop_duplicates(subset=["Author Profile URL", "Post Text"], inplace=True)

    csv_path = os.path.join(out_dir, "posts.csv")
    xlsx_path = os.path.join(out_dir, "posts.xlsx")
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    try:
        df.to_excel(xlsx_path, index=False)
    except Exception as e:
        print(f"   ! Excel export failed ({e}). Install openpyxl: pip install openpyxl")
        xlsx_path = "(skipped)"
    print(f"\nDone. {len(df)} unique posts.\n  {csv_path}\n  {xlsx_path}")


def main():
    ap = argparse.ArgumentParser(description="LinkedIn Content Search scraper (Posts, past 24h)")
    ap.add_argument("--keyword", help="single keyword to search")
    ap.add_argument("--keyword-file", help="file with one keyword per line (# = comment)")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output directory")
    ap.add_argument("--headless", action="store_true",
                    help="run without a visible window (only after logging in once)")
    ap.add_argument("--content-type", choices=["all", "jobs"], default="all",
                    help="'jobs' keeps only hiring/job posts; 'all' keeps everything "
                         "(the 'Is Job Post' column is filled either way)")
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
