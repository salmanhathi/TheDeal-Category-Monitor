from flask import Flask, render_template, request, jsonify, Response
import requests
from bs4 import BeautifulSoup
import csv, io, re, time, json
from urllib.parse import urlparse, urljoin, urlunparse, parse_qs, urlencode, urldefrag
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

SCAN_WORKERS   = 20   # parallel product scans
IMG_CHECK_WORKERS = 8  # parallel image-accessibility checks *per product*
CRAWL_DELAY    = 0.2  # seconds between category page fetches

# Shared, connection-pooled session — reused across all requests so repeated
# hits to the same host (your own store) reuse TCP/TLS connections instead of
# opening a new one every time. Meaningfully faster on large scans.
SESSION = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=1)
SESSION.mount('https://', _adapter)
SESSION.mount('http://', _adapter)

# HTTP statuses worth retrying — rate limits and transient server errors.
# A plain 404/410/403 is not retried: retrying won't change a real error.
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
MAX_FETCH_RETRIES  = 3   # attempts AFTER the first — so 4 tries total
BASE_BACKOFF       = 1.0  # seconds; doubles each retry (1s, 2s, 4s), capped below
MAX_BACKOFF        = 12.0


def fetch_with_retry(url, timeout=15):
    """
    GET with retry/backoff for transient failures:
      - 429 / 500 / 502 / 503 / 504  → retry with exponential backoff,
        honoring the server's Retry-After header when it sends one.
      - Timeout / ConnectionError    → retry with exponential backoff.
    A genuine error (404, 403, etc.) or a request that still fails after
    MAX_FETCH_RETRIES is returned/raised as-is for the caller to record.
    """
    delay = BASE_BACKOFF
    last_response = None
    for attempt in range(MAX_FETCH_RETRIES + 1):
        try:
            r = SESSION.get(url, headers=HEADERS, timeout=timeout, verify=False)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            if attempt == MAX_FETCH_RETRIES:
                raise
            time.sleep(delay)
            delay = min(delay * 2, MAX_BACKOFF)
            continue

        last_response = r
        if r.status_code in RETRYABLE_STATUSES and attempt < MAX_FETCH_RETRIES:
            wait = delay
            retry_after = r.headers.get('Retry-After')
            if retry_after:
                try:
                    wait = min(float(retry_after), MAX_BACKOFF)
                except ValueError:
                    pass
            time.sleep(wait)
            delay = min(delay * 2, MAX_BACKOFF)
            continue

        return r
    return last_response

# ─────────────────────────────────────────────────────────────────────────────
# CATEGORY CRAWLER
# ─────────────────────────────────────────────────────────────────────────────

# Static/content pages that end in .html but are never products.
# Add to this list if the crawler picks up other footer/info pages.
NON_PRODUCT_PATH_SIGNALS = [
    'about-us', 'about', 'contact-us', 'contact', 'faq',
    'privacy-policy', 'privacy', 'terms-conditions', 'terms',
    'order-shipping', 'return-refunds', 'returns', 'refund',
    'shipping', 'delivery-info', 'sitemap', 'store-locator',
    'careers', 'blog', 'news', 'help', 'customer-service',
    'size-guide', 'size-chart', 'gift-card', 'login', 'register',
    'my-account', 'cart', 'checkout', 'wishlist', 'track-order',
]


def is_product_url(url, base_domain):
    parsed = urlparse(url)
    if parsed.netloc and parsed.netloc != base_domain:
        return False
    path = parsed.path.lower()

    if any(f'/{sig}' in path for sig in NON_PRODUCT_PATH_SIGNALS):
        return False

    product_hints = ['/p/', '/product/', '/products/', '/item/', '/pd/']
    if any(h in path for h in product_hints):
        return True
    category_hints = ['/c/', '/category/', '/search', '/s/', 'cgid=', '/new-in', '/women', '/men', '/kids']
    if path.endswith('.html') and not any(h in url for h in category_hints):
        return True
    return False


def extract_product_links(soup, base_url, base_domain):
    links = set()
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if not href or href.startswith('#') or href.startswith('javascript'):
            continue
        full = urljoin(base_url, href)
        full, _ = urldefrag(full)
        if is_product_url(full, base_domain):
            links.add(full)
    return links


def crawl_category(category_url, page_size=48, max_pages=75):
    parsed      = urlparse(category_url)
    base_domain = parsed.netloc
    qs = parse_qs(parsed.query, keep_blank_values=True)
    qs.pop('start', None)
    if 'sz' in qs:
        try:
            page_size = int(qs['sz'][0])
        except ValueError:
            pass
    qs['sz'] = [str(page_size)]

    all_products = set()
    logs = []
    start = 0

    for page_num in range(max_pages):
        qs['start'] = [str(start)]
        paged_qs  = urlencode({k: v[0] for k, v in qs.items()})
        page_url  = urlunparse(parsed._replace(query=paged_qs))
        logs.append(f"Fetching page {page_num + 1}: {page_url}")
        try:
            r = SESSION.get(page_url, headers=HEADERS, timeout=15, verify=False)
            if r.status_code != 200:
                logs.append(f"  → HTTP {r.status_code}, stopping.")
                break
            soup  = BeautifulSoup(r.text, 'html.parser')
            found = extract_product_links(soup, page_url, base_domain)
            new   = found - all_products
            logs.append(f"  → Found {len(found)} links ({len(new)} new)")
            if not new:
                logs.append("  → No new products — pagination complete.")
                break
            all_products.update(new)
            start += page_size
            time.sleep(CRAWL_DELAY)
        except Exception as e:
            logs.append(f"  → Error: {e}")
            break

    return sorted(all_products), logs


# ─────────────────────────────────────────────────────────────────────────────
# EXTRACTORS  —  tuned for thedealoutlet.com SFCC structure
# ─────────────────────────────────────────────────────────────────────────────

def extract_prices(soup):
    """
    Returns dict: { sale_price, original_price, saving, has_sale }
    Targets TDO structure:
      Sale:     h2.sales span.value[content]
      Original: span.strike-through.list span.value[content]
      Saving:   .wis_fiyatfark
    Falls back to generic selectors for other sites.
    """
    result = {
        "sale_price":     None,
        "original_price": None,
        "saving":         None,
        "has_sale":       False,
    }

    # ── TDO / SFCC specific ──────────────────────────────────────────────────
    sale_el = soup.select_one('h2.sales span.value, .sales span.value')
    if sale_el:
        result["sale_price"] = sale_el.get('content') or re.sub(r'[^\d.]', '', sale_el.get_text())

    orig_el = soup.select_one('span.strike-through.list span.value, .strike-through .value')
    if orig_el:
        result["original_price"] = orig_el.get('content') or re.sub(r'[^\d.]', '', orig_el.get_text())

    if result["sale_price"] and result["original_price"]:
        result["has_sale"] = True
        _compute_saving(result)
        return result

    # ── Generic fallbacks ────────────────────────────────────────────────────
    if not result["sale_price"]:
        for sel in ['[itemprop="price"]', '.price-sales', '.special-price .price',
                    'meta[property="product:price:amount"]']:
            el = soup.select_one(sel)
            if el:
                v = el.get('content') or re.sub(r'[^\d.]', '', el.get_text())
                if v:
                    result["sale_price"] = v
                    break

    if not result["original_price"]:
        for sel in ['.price-was', '.regular-price .price', '.old-price .price',
                    '.price__compare']:
            el = soup.select_one(sel)
            if el:
                v = re.sub(r'[^\d.]', '', el.get_text())
                if v:
                    result["original_price"] = v
                    break

    # JSON-LD last resort
    if not result["sale_price"]:
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                data = json.loads(script.string or '')
                if isinstance(data, dict):
                    offers = data.get('offers', {})
                    if isinstance(offers, dict):
                        p = offers.get('price') or offers.get('lowPrice')
                        if p:
                            result["sale_price"] = str(p)
                            break
            except Exception:
                pass

    result["has_sale"] = bool(result["sale_price"] and result["original_price"])
    _compute_saving(result)
    return result


def _compute_saving(result):
    """Saving is always derived (original vs sale), never scraped —
    the site's own 'saving' badge text was unreliable/inconsistent.
    Expressed as a percentage, e.g. '54%'."""
    try:
        orig = float(result["original_price"])
        sale = float(result["sale_price"])
        if orig <= 0:
            result["saving"] = None
            return
        pct = round((orig - sale) / orig * 100)
        result["saving"] = f"{pct}%" if pct > 0 else None
    except (TypeError, ValueError):
        result["saving"] = None


def extract_images(soup, base_url):
    """
    Priority order:
    1. TDO/SFCC product image containers (desktop + mobile)
    2. og:image meta
    3. schema.org itemprop=image
    4. Common CMS class names
    Filters out SVGs, data URIs, and TDO's 'noimagelarge.png' placeholder.
    """
    images = []
    seen   = set()

    # TDO SFCC serves this when no product image exists — treat as missing
    PLACEHOLDER_SIGNALS = [
        'noimagelarge',
        'noimage',
        'no_image',
        '/default/dw58870029/',   # TDO's specific no-image asset hash
        'blank.gif', '1x1', 'pixel',
    ]

    def is_placeholder(tag, src):
        alt   = (tag.get('alt')   or '').strip().lower()
        title = (tag.get('title') or '').strip().lower()
        if alt in ('no image', 'noimage') or title in ('no image', 'noimage'):
            return True
        low = src.lower()
        return any(p in low for p in PLACEHOLDER_SIGNALS)

    def dedup_key(full):
        """Same photo often appears twice with different URLs — a small
        thumbnail plus the full-size image, or a desktop vs mobile variant —
        e.g. .../medium/photo.jpg vs .../large/photo.jpg, or the same path
        with a different ?sw=/?sh= resize query. Normalize those away so
        they count as ONE image instead of two."""
        parsed = urlparse(full)
        path = re.sub(r'/(large|medium|small|thumb(?:nail)?s?|zoom|swatch)/', '/', parsed.path, flags=re.I)
        path = re.sub(r'[_-](large|medium|small|thumb|thumbnail|zoom)(?=\.\w+$)', '', path, flags=re.I)
        return path.lower()

    def add(tag, src=None):
        if tag is None:
            # called with just a URL string (og:image)
            if not src:
                return
            full = urljoin(base_url, src.strip())
            low = full.lower()
            if any(x in low for x in ['data:', '.svg'] + PLACEHOLDER_SIGNALS):
                return
            key = dedup_key(full)
            if key in seen:
                return
            seen.add(key)
            images.append(full)
            return

        raw = (tag.get('src') or tag.get('data-src') or tag.get('data-zoom-image')
               or tag.get('data-lazy') or tag.get('data-original') or tag.get('content') or '')
        if not raw:
            return
        full = urljoin(base_url, raw.strip())
        low = full.lower()
        if any(x in low for x in ['data:', '.svg']):
            return
        if is_placeholder(tag, full):
            return
        key = dedup_key(full)
        if key in seen:
            return
        seen.add(key)
        images.append(full)

    # TDO desktop: most reliable — real images only, no carousel clones
    for tag in soup.select('.product-images-desktop img, .js-img-parent-div img'):
        add(tag)

    # TDO/SFCC generic containers
    for sel in [
        '.primary-images img',
        '.pdp-images img',
        '.image-container img',
        '[data-image-role="product"]',
        '.product-gallery img',
    ]:
        for tag in soup.select(sel):
            add(tag)

    # og:image — skip if it's also a placeholder URL
    og = soup.find('meta', property='og:image')
    if og and og.get('content'):
        src = og['content']
        if not any(p in src.lower() for p in PLACEHOLDER_SIGNALS):
            add(None, src)

    # schema.org itemprop=image (TDO uses this too, but slick clones duplicates — deduped by seen set)
    for el in soup.select('[itemprop="image"]'):
        add(el)

    # generic fallbacks
    for sel in ['.carousel img', '.slick-slide img', '.swiper-slide img']:
        for tag in soup.select(sel):
            add(tag)

    return images


def check_image_accessible(url):
    try:
        r = SESSION.head(url, headers=HEADERS, timeout=4, allow_redirects=True, verify=False)
        return r.status_code == 200
    except Exception:
        return False


def extract_title(soup):
    og = soup.find('meta', property='og:title')
    if og and og.get('content'):
        return og['content'].strip()
    h1 = soup.find('h1')
    if h1:
        return h1.get_text(strip=True)
    return soup.title.string.strip() if soup.title else None


def extract_description(soup):
    meta = soup.find('meta', attrs={'name': 'description'})
    if meta and meta.get('content'):
        return meta['content'].strip()
    og = soup.find('meta', property='og:description')
    if og and og.get('content'):
        return og['content'].strip()
    for sel in ['[itemprop="description"]', '.product-description',
                '.pdp-description', '.product__description', '.short-description']:
        el = soup.select_one(sel)
        if el:
            txt = el.get_text(strip=True)
            if txt:
                return txt[:300]
    return None


# ─────────────────────────────────────────────────────────────────────────────
# ANALYSER
# ─────────────────────────────────────────────────────────────────────────────

def analyse_url(url):
    result = {
        "url": url, "title": None,
        "sale_price": None, "original_price": None, "saving": None, "has_sale": False,
        "images": [], "image_count": 0, "broken_images": [],
        "description": None, "issues": [], "status": "ok",
        "http_status": None,
        "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        r = fetch_with_retry(url)
        result["http_status"] = r.status_code
        if r.status_code != 200:
            label = f"HTTP {r.status_code}"
            if r.status_code == 429:
                label = "HTTP 429 (rate limited)"
            elif r.status_code in RETRYABLE_STATUSES:
                label += " (still failing after retries)"
            result["issues"].append(label)
            result["status"] = "error"
            return result

        soup = BeautifulSoup(r.text, 'html.parser')

        result["title"]       = extract_title(soup)
        result["description"] = extract_description(soup)
        result["images"]      = extract_images(soup, url)
        result["image_count"] = len(result["images"])

        prices = extract_prices(soup)
        result.update(prices)

        # ── Issue checks ──────────────────────────────────────────────────────
        if not result["title"]:
            result["issues"].append("Missing title")

        if not result["sale_price"] and not result["original_price"]:
            result["issues"].append("Missing price")
        elif not result["sale_price"]:
            result["issues"].append("Missing sale price")
        elif not result["original_price"]:
            result["issues"].append("Missing original price")

        if result["image_count"] == 0:
            result["issues"].append("No images found")
        else:
            with ThreadPoolExecutor(max_workers=min(IMG_CHECK_WORKERS, len(result["images"]))) as img_ex:
                ok_flags = list(img_ex.map(check_image_accessible, result["images"]))
            broken = [img for img, ok in zip(result["images"], ok_flags) if not ok]
            result["broken_images"] = broken
            if broken:
                result["issues"].append(f"{len(broken)} broken image(s)")

        if not result["description"]:
            result["issues"].append("Missing description")

        result["status"] = "issues" if result["issues"] else "ok"

    except requests.exceptions.Timeout:
        result["issues"].append("Timeout (>15s)"); result["status"] = "error"
    except requests.exceptions.ConnectionError:
        result["issues"].append("Connection error"); result["status"] = "error"
    except Exception as e:
        result["issues"].append(f"Error: {str(e)[:80]}"); result["status"] = "error"

    return result


def build_summary(results):
    return {
        "total":                len(results),
        "ok":                   sum(1 for r in results if r["status"] == "ok"),
        "issues":               sum(1 for r in results if r["status"] == "issues"),
        "errors":               sum(1 for r in results if r["status"] == "error"),
        "missing_price":        sum(1 for r in results if "Missing price" in r["issues"]),
        "missing_sale_price":   sum(1 for r in results if "Missing sale price" in r["issues"]),
        "missing_orig_price":   sum(1 for r in results if "Missing original price" in r["issues"]),
        "missing_images":       sum(1 for r in results if "No images found" in r["issues"]),
        "broken_images":        sum(1 for r in results if any("broken image" in i for i in r["issues"])),
        "missing_desc":         sum(1 for r in results if "Missing description" in r["issues"]),
    }


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/health')
def health():
    """Lightweight liveness check for the TDO Toolbox hub's status dot —
    just confirms the service is up, does no scraping."""
    return jsonify({"status": "ok", "service": "product-health-monitor"})


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/discover', methods=['POST'])
def discover():
    data         = request.get_json()
    category_url = (data.get('category_url') or '').strip()
    if not category_url:
        return jsonify({"error": "No category URL provided"}), 400
    if not category_url.startswith('http'):
        category_url = 'https://' + category_url
    page_size = int(data.get('page_size', 48))
    max_pages = int(data.get('max_pages', 75))
    product_urls, logs = crawl_category(category_url, page_size, max_pages)
    return jsonify({"product_urls": product_urls, "logs": logs, "count": len(product_urls)})


@app.route('/scan', methods=['POST'])
def scan():
    data = request.get_json()
    urls = [u.strip() for u in (data.get('urls') or '').splitlines() if u.strip()]
    if not urls:
        return jsonify({"error": "No URLs provided"}), 400
    if len(urls) > 5000:
        return jsonify({"error": "Max 5000 URLs per scan"}), 400

    results = [None] * len(urls)

    def worker(idx, url):
        if not url.startswith('http'):
            url = 'https://' + url
        return idx, analyse_url(url)

    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        futures = {ex.submit(worker, i, u): i for i, u in enumerate(urls)}
        for future in as_completed(futures):
            idx, res = future.result()
            results[idx] = res

    return jsonify({"results": results, "summary": build_summary(results)})


@app.route('/export-csv', methods=['POST'])
def export_csv():
    data    = request.get_json()
    results = data.get('results', [])
    output  = io.StringIO()
    writer  = csv.DictWriter(output, fieldnames=[
        'url', 'title', 'sale_price', 'original_price', 'saving',
        'image_count', 'broken_images', 'description_present',
        'issues', 'status', 'http_status', 'scraped_at'
    ])
    writer.writeheader()
    for r in results:
        writer.writerow({
            'url':                  r.get('url', ''),
            'title':                r.get('title', ''),
            'sale_price':           r.get('sale_price', ''),
            'original_price':       r.get('original_price', ''),
            'saving':               r.get('saving', ''),
            'image_count':          r.get('image_count', 0),
            'broken_images':        '; '.join(r.get('broken_images', [])),
            'description_present':  'Yes' if r.get('description') else 'No',
            'issues':               '; '.join(r.get('issues', [])),
            'status':               r.get('status', ''),
            'http_status':          r.get('http_status', ''),
            'scraped_at':           r.get('scraped_at', ''),
        })
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Response(output.getvalue(), mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=product_health_{ts}.csv'})


if __name__ == '__main__':
    # use_reloader=False: the auto-reloader restarts the whole server whenever
    # it thinks a file changed. On Microsoft Store Python installs the
    # WindowsApps folder's file virtualization makes stdlib files (e.g.
    # netrc.py) look "changed" even when they aren't, causing false-positive
    # restarts. A restart mid-crawl/scan drops the in-flight request, which is
    # what surfaces as the "Discovery failed" / "Scan failed" alert. If you
    # need the reloader while actively editing this file, re-enable it, but
    # know a long scan can get killed by a spurious reload.
    # threaded=True: lets Flask's dev server handle multiple requests at once
    # (e.g. several /discover calls for different categories fired together).
    # Without it, the dev server is single-threaded and would just queue them.
    app.run(debug=True, port=5050, use_reloader=False, threaded=True)
