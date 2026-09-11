# Usage Guidelines

## Crawling etiquette (even against your own site)

- This tool hits your live storefront with real HTTP requests. High
  concurrency against your own server/CDN can still trip rate limits, spike
  load, or crowd out real customer traffic during peak hours.
- Start a new/unfamiliar category at the default concurrency (`SCAN_WORKERS =
  20`) before raising it. If you see a wave of `429`s in the results even
  after the automatic retries, that's the server telling you to back off —
  lower `SCAN_WORKERS` rather than adding more retries.
- Prefer running large, multi-thousand-product scans outside business peak
  hours if your infrastructure is sensitive to load.
- `CRAWL_DELAY` (category pagination) is intentionally polite (0.2s between
  pages) — don't remove it just to save a few seconds on discovery.

## Category tab vs. Manual tab

- **Category tab** is for *listing/category* pages — the kind with
  pagination (`?start=`, `?sz=`) and many products. This is where crawling
  and discovery happens.
- **Manual tab** is for pasting *direct product page* URLs you already have
  — e.g. a spot-check list, or links from a report elsewhere. Don't paste
  category URLs here; the scanner will try to read it as a single product
  page and report it as broken/missing content, since it isn't one.

## Interpreting results

- **Status "error"** means the page itself never loaded properly (HTTP
  error, timeout, connection error) — these are the ones **Re-Run Error
  URLs** targets. Re-running is worth trying once or twice; if a URL keeps
  failing, it's likely genuinely broken (410/404) rather than transient.
- **Status "issues"** means the page loaded fine but is missing something
  (price, image, description). Re-running won't fix these — they need a
  catalog/content fix on the product itself.
- **Saving %** is always calculated (`(original − sale) / original`), never
  scraped from the page — treat it as a derived figure, not a copy of
  whatever badge text your PDP shows.
- **Image count** is deduplicated across differently-sized URLs of the same
  photo. If you add a new CDN/image path convention that doesn't match the
  existing size-folder patterns (`large/medium/small/thumb/zoom/swatch`),
  double-counting can reappear for that pattern — extend the `dedup_key()`
  regex in `extract_images()` if so.

## Before exporting to the team / using results to update the catalog

- Spot-check a handful of flagged rows manually before trusting the CSV
  wholesale — selector-based extraction (prices, images) is tuned for TDO's
  current SFCC markup and can silently miss things if the template changes.
- If you add or rename a footer/content page (About Us style), and it starts
  appearing in crawl results as a false "product," add its slug to
  `NON_PRODUCT_PATH_SIGNALS` in `app.py`.

## Extending safely

- Raising `MAX_CATEGORIES` (in `index.html`) or `SCAN_WORKERS` (in `app.py`)
  is safe to do, but raise gradually and watch for `429`s rather than
  jumping straight to a large number.
- Any new "size" naming convention for product images should be added to the
  `dedup_key()` regex, not worked around downstream.
