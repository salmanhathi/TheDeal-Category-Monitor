# Product Health Monitor

A local Flask tool that crawls TDO (or any SFCC-style) category pages, discovers
product URLs, scans each product page for data-quality issues (missing
title/price/description, missing or broken images), and exports the results
to CSV.

## Features

- **Multi-category crawl** — add up to 5 category URLs and discover products
  from all of them at once (concurrently, not one after another).
- **Auto-pagination** — walks each category's `?start=`/`?sz=` pages until no
  new products turn up; a high "max pages" ceiling is safe even for small
  categories, since it stops early on its own.
- **Non-product filtering** — footer/info pages (About Us, FAQ, Terms,
  Privacy, Returns, etc.) are excluded from crawl results automatically.
- **Product health scan** — for every discovered (or manually pasted) URL,
  checks:
  - Title, description
  - Sale price / original price (with **saving %** computed as
    `(original − sale) / original`, not scraped)
  - Image count, with duplicate detection across differently-sized URLs of
    the same photo (e.g. thumbnail vs. full-size)
  - Broken images (checked in parallel per product)
- **Resilient scanning** — transient failures (`429`, `500`, `502`, `503`,
  `504`, timeouts, connection errors) are retried automatically with
  exponential backoff, honoring a server's `Retry-After` header on 429s.
- **Re-Run Error URLs** — after a scan, one click re-scans only the URLs that
  ended in an error (not ones that just had missing content), and merges the
  new results back into the table in place.
- **CSV export** of the full results table.

## Project structure

```
your-project/
├── app.py
├── requirements.txt
└── templates/
    └── index.html
```

`app.py` calls `render_template('index.html')`, so `index.html` **must**
live in a `templates/` folder next to `app.py` — Flask won't find it
otherwise.

## Setup

```bash
# 1. Create and activate a virtual environment (recommended)
python -m venv venv
venv\Scripts\activate        # Windows
source venv/bin/activate     # macOS/Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run
python app.py
```

Then open **http://127.0.0.1:5050** in your browser.

> **Windows note:** if you're on the Microsoft Store build of Python, you may
> see the dev server spontaneously restart (`Detected change in ... netrc.py,
> reloading`). This is a known false-positive with that Python distribution's
> file virtualization, not a bug in this app. The app already runs with
> `use_reloader=False` to prevent it from killing an in-progress crawl/scan;
> if you ever re-enable the reloader for active development, know that a long
> scan can get interrupted by a spurious reload. Installing Python from
> [python.org](https://python.org) avoids the issue entirely.

## Usage

### Category tab
1. **Step 1 — Discover:** Enter one category URL, or click **+ Add
   Category** (up to 5) to crawl several at once. Set page size (`sz=`) and
   max pages to crawl if the defaults (48 / 75) don't fit your site. Click
   **Discover Products**.
2. **Step 2 — Scan:** Once products are discovered, click **Start Health
   Scan** to check every product page.
3. Review results — filter by status/issue type, use **Re-Run Error URLs**
   for anything that failed outright, then **Export CSV**.

### Manual tab
Paste direct **product page URLs** (one per line, max 500) — e.g.:
```
https://www.thedealoutlet.com/ae-en/jackson-hole-pilot-sunglasses/023327157517.html
```
This tab is for individual product links, not category/listing pages —
category URLs belong in the Category tab's discovery step.

## Key settings (in `app.py`)

| Constant | Default | What it controls |
|---|---|---|
| `SCAN_WORKERS` | 20 | Parallel product-page scans |
| `IMG_CHECK_WORKERS` | 8 | Parallel image-accessibility checks per product |
| `MAX_FETCH_RETRIES` | 3 | Retry attempts after the first, for transient failures |
| `BASE_BACKOFF` / `MAX_BACKOFF` | 1s / 12s | Retry backoff timing |
| `RETRYABLE_STATUSES` | `{429, 500, 502, 503, 504}` | HTTP codes that get retried |
| `NON_PRODUCT_PATH_SIGNALS` | see `app.py` | URL slugs excluded from crawl results |

In `index.html` (`<script>` block):

| Constant | Default | What it controls |
|---|---|---|
| `MAX_CATEGORIES` | 5 | Max category rows you can add for a single discovery run |

## Notes

- This is a local dev tool (Flask's built-in server), not meant to be
  exposed to the internet as-is. There's no authentication and
  `verify=False` is used for TLS checks against your own store's requests.
- If you see frequent `429`s even after the built-in retries, your own
  server/CDN is likely capping concurrent connections from one client —
  lower `SCAN_WORKERS` (e.g. to 10–12) rather than raising retries further.
