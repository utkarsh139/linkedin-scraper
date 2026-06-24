# LinkedIn Content Scraper

A small utility that searches **LinkedIn Content Search**, filters to **Posts**
from the **last 24 hours**, scrolls to load everything, and exports to
`out/posts.xlsx` + `out/posts.csv`.

LinkedIn's current search build renders posts as web-components with hashed,
per-build CSS class names and no activity-id attributes, so the scraper extracts
from the rendered text instead: each result card's `innerText` begins with
"Feed post", which we use to isolate cards and then parse author, headline, and
post body. `debug_scrape.py` is a diagnostic that dumps the live DOM/network if
LinkedIn changes its markup again.

This is a standalone personal utility — **not** part of any other project.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

## First run (log in once)

```bash
python linkedin_scraper.py --keyword "AI Engineer"
```

A Chrome window opens. Log in to LinkedIn (complete any 2FA), then press Enter
in the terminal. Your session is saved in `./li_profile/` and reused on every
later run — you won't need to log in again.

## Usage

```bash
# single keyword
python linkedin_scraper.py --keyword "Machine Learning Engineer"

# many keywords from a file
python linkedin_scraper.py --keyword-file keywords.txt

# no args -> uses the built-in default keyword list
python linkedin_scraper.py

# once logged in, you can run without a visible window
python linkedin_scraper.py --keyword-file keywords.txt --headless
```

## Output

`out/posts.xlsx` and `out/posts.csv`, one row per unique post:

Search Keyword · Author Name · Author Headline · Post Text · Author Profile URL ·
Post Date · Reactions Count · Comments Count · Reposts Count · Scraped At

Duplicates (same post across multiple keywords) are removed automatically,
keyed on author + post text.

## Notes

- `Post Date` is the relative time LinkedIn shows (e.g. `now`, `4m`, `2h`),
  since the search view exposes nothing more precise.
- Engagement counts (reactions / comments / reposts) are best-effort and are
  usually `0` — LinkedIn's search results rarely render them for recent posts.
- There is no per-post permalink in the search DOM, so `Author Profile URL`
  is captured instead of a post URL.
- Keep volume reasonable and pacing human-like; heavy scraping can get a
  LinkedIn account flagged. Use an account you're comfortable using for this.
- If LinkedIn changes its markup again and you get `0 found`, run
  `python debug_scrape.py` — it dumps the live DOM and network to `out/debug/`
  so the card-detection logic in `DOM_EXTRACT_JS` / `parse_card` can be adjusted.
```
