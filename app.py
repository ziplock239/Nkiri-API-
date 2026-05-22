"""
Nkiri API
=========
Endpoints:
  GET /api/search?q=query           - search movies
  GET /api/movies                   - all / latest movies (paginated)
  GET /api/movie?url=PAGE_URL       - single movie info
  GET /api/download?url=PAGE_URL    - fresh direct download links (live, never cached)
"""

import re
import time
import logging
from urllib.parse import urljoin, urlparse

import cloudscraper
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# ─── Setup ────────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)
limiter = Limiter(get_remote_address, app=app, default_limits=["200 per hour"], storage_uri="memory://")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("nkiri")

BASE = "https://thenkiri.com.ng"
FILE_EXT_RE = re.compile(r'\.(mp4|mkv|avi|mov|webm)(\?|#|$)', re.I)

# ─── Session ──────────────────────────────────────────────────────────────────
def session():
    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False},
        delay=5
    )
    s.headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
    })
    return s

def get(s, url, **kw):
    kw.setdefault("timeout", 30)
    s.headers.update({"Referer": BASE})
    r = s.get(url, **kw)
    r.raise_for_status()
    return r

def post(s, url, data, referer=None, **kw):
    kw.setdefault("timeout", 30)
    s.headers.update({
        "Referer": referer or url,
        "Origin": urlparse(url).scheme + "://" + urlparse(url).netloc,
        "Content-Type": "application/x-www-form-urlencoded",
    })
    r = s.post(url, data=data, **kw)
    r.raise_for_status()
    return r

def is_nkiri(url):
    return "thenkiri.com.ng" in urlparse(url).netloc

# ─── Parse helpers ────────────────────────────────────────────────────────────
def parse_movie_cards(soup) -> list[dict]:
    """Extract movie cards from any listing page."""
    movies = []
    seen = set()

    # Nkiri uses standard WordPress post structure
    for article in soup.select("article"):
        a = article.select_one("a[rel='bookmark'], .entry-title a, h2 a, h3 a, a")
        if not a:
            continue
        href = a.get("href", "")
        if not href.startswith("http"):
            href = urljoin(BASE, href)
        if href in seen:
            continue
        # Skip category / tag / page / search links
        path = urlparse(href).path.strip("/")
        if not path or "/" in path:
            continue
        if any(x in href for x in ["/category/", "/tag/", "/page/", "/?", "#"]):
            continue
        seen.add(href)

        title = a.get_text(strip=True) or a.get("title", "")
        thumb = ""
        img = article.select_one("img[data-src], img[src]")
        if img:
            thumb = img.get("data-src") or img.get("data-lazy-src") or img.get("src", "")

        movies.append({"title": title, "url": href, "thumbnail": thumb})

    # Fallback: if articles not found, try generic anchor scan
    if not movies:
        for a in soup.select(".entry-title a, h2.post-title a, .post-thumbnail a"):
            href = a.get("href", "")
            if not href.startswith("http"):
                href = urljoin(BASE, href)
            if href in seen:
                continue
            path = urlparse(href).path.strip("/")
            if not path or "/" in path:
                continue
            seen.add(href)
            title = a.get_text(strip=True)
            movies.append({"title": title, "url": href, "thumbnail": ""})

    return movies


def parse_movie_info(soup, page_url: str) -> dict:
    title = ""
    for sel in ["h1.entry-title", "h1.post-title", "h1"]:
        el = soup.select_one(sel)
        if el:
            title = el.get_text(strip=True)
            break

    thumbnail = ""
    og = soup.find("meta", property="og:image")
    if og:
        thumbnail = og.get("content", "")
    if not thumbnail:
        img = soup.select_one(".entry-content img, .post-thumbnail img, article img")
        if img:
            thumbnail = img.get("data-src") or img.get("src", "")

    description = ""
    og_desc = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "description"})
    if og_desc:
        description = og_desc.get("content", "").strip()

    details = {}
    content = soup.select_one(".entry-content, .post-content")
    if content:
        for row in content.select("table tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) == 2:
                k = cells[0].get_text(strip=True).lower().replace(" ", "_").rstrip(":")
                v = cells[1].get_text(strip=True)
                if k and v and len(k) < 40:
                    details[k] = v
        # Also parse strong: value patterns
        for p in content.select("p"):
            text = p.get_text(" ", strip=True)
            m = re.match(r"^([A-Za-z ]{2,25}):\s*(.+)$", text)
            if m:
                k = m.group(1).strip().lower().replace(" ", "_")
                details[k] = m.group(2).strip()[:200]

    return {
        "title": title,
        "url": page_url,
        "thumbnail": thumbnail,
        "description": description,
        "details": details,
    }


# ─── Downloadwella resolver (XFilesharing Pro engine) ────────────────────────
def resolve_downloadwella(s, page_url: str) -> dict | None:
    """
    XFilesharing Pro two-step download flow:
      Step 1: GET page  → extract hidden form fields (op, id, fname, hash, etc.)
      Step 2: POST with op=download1 → get countdown page with more hidden fields
      Step 3: POST with op=download2 → get redirect or direct link
    Returns {"url": "...", "filename": "...", "size": "..."} or None
    """
    log.info(f"  Resolving downloadwella: {page_url}")
    file_host_base = urlparse(page_url).scheme + "://" + urlparse(page_url).netloc

    try:
        # ── Step 1: GET the file page ──────────────────────────────────────
        r1 = get(s, page_url)
        soup1 = BeautifulSoup(r1.text, "lxml")

        # Extract filename and size from page
        filename = ""
        size = ""
        fname_el = soup1.select_one(".name, .file_name, #file_title, h2, title")
        if fname_el:
            filename = fname_el.get_text(strip=True)
        size_el = soup1.select_one(".size, .file_size, #file_size")
        if size_el:
            size = size_el.get_text(strip=True)

        # Collect all hidden inputs from the free download form
        form = (
            soup1.select_one("form[name='F1']")
            or soup1.select_one("form")
        )
        if not form:
            log.warning("  No form found on downloadwella page")
            return None

        form_data = {}
        for inp in form.find_all("input"):
            name = inp.get("name", "")
            value = inp.get("value", "")
            if name:
                form_data[name] = value

        # Make sure we're doing free download
        form_data["op"] = "download1"
        form_data.pop("method_premium", None)
        form_data["method_free"] = "Free Download"

        form_action = form.get("action") or page_url
        if not form_action.startswith("http"):
            form_action = urljoin(file_host_base, form_action)

        # ── Step 2: POST download1 → countdown/confirmation page ──────────
        time.sleep(1)  # brief pause
        r2 = post(s, form_action, data=form_data, referer=page_url)
        soup2 = BeautifulSoup(r2.text, "lxml")

        # Check for direct link in this response already
        direct = _find_direct_link(soup2)
        if direct:
            return {"url": direct, "filename": filename, "size": size}

        # Extract hidden fields for step 3
        form2 = (
            soup2.select_one("form[name='F1']")
            or soup2.select_one("form[id='F1']")
            or soup2.select_one("form")
        )
        if not form2:
            # Sometimes the link is right in the page without a second form
            log.warning("  No second form on downloadwella — checking for link")
            return None

        form2_data = {}
        for inp in form2.find_all("input"):
            name = inp.get("name", "")
            value = inp.get("value", "")
            if name:
                form2_data[name] = value

        form2_data["op"] = "download2"
        form2_data.pop("method_premium", None)
        form2_data["method_free"] = "Free Download"

        form2_action = form2.get("action") or page_url
        if not form2_action.startswith("http"):
            form2_action = urljoin(file_host_base, form2_action)

        # ── Step 3: POST download2 → actual file link ──────────────────────
        wait = _parse_countdown(soup2)
        if wait > 0:
            log.info(f"  Waiting {wait}s countdown...")
            time.sleep(min(wait, 15))  # cap at 15s for API responsiveness

        r3 = post(s, form2_action, data=form2_data, referer=r2.url)
        soup3 = BeautifulSoup(r3.text, "lxml")

        # Check for redirect
        if r3.history:
            final_url = r3.url
            if FILE_EXT_RE.search(final_url):
                return {"url": final_url, "filename": filename, "size": size}

        direct = _find_direct_link(soup3)
        if direct:
            return {"url": direct, "filename": filename, "size": size}

        # Last resort: check response headers for Location
        loc = r3.headers.get("Location", "")
        if loc and FILE_EXT_RE.search(loc):
            return {"url": loc, "filename": filename, "size": size}

        log.warning("  Could not extract direct link from downloadwella")
        return None

    except Exception as e:
        log.error(f"  Downloadwella resolve error: {e}")
        return None


def _find_direct_link(soup) -> str:
    """Search a soup for a direct .mp4/.mkv/.avi link."""
    # In <a> tags
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if FILE_EXT_RE.search(href) and href.startswith("http"):
            return href
    # In scripts
    for script in soup.find_all("script"):
        text = script.string or ""
        m = re.search(r'["\']((https?://[^"\']{10,}\.(?:mp4|mkv|avi|webm))[^"\']*)["\']', text, re.I)
        if m:
            return m.group(1)
    # Direct URL in page text
    m = re.search(r'(https?://\S+\.(?:mp4|mkv|avi|webm))(?:\s|")', soup.get_text())
    if m:
        return m.group(1)
    return ""


def _parse_countdown(soup) -> int:
    """Extract countdown seconds from XFilesharing countdown page."""
    # Common patterns: id="countdown", var countdown = 30, etc.
    for script in soup.find_all("script"):
        text = script.string or ""
        m = re.search(r'(?:countdown|count|wait)\s*[=:]\s*(\d+)', text, re.I)
        if m:
            return int(m.group(1))
    el = soup.select_one("#countdown, .countdown, #timer")
    if el:
        m = re.search(r'\d+', el.get_text())
        if m:
            return int(m.group(0))
    return 5  # default safe wait


# ─── Main download resolver (called per movie page) ───────────────────────────
def resolve_download_links(s, page_url: str) -> list[dict]:
    """
    Two-stage resolution:
      Stage 1: Scrape Nkiri movie page → find downloadwella.com links
      Stage 2: For each downloadwella link → POST through XFilesharing → get direct file URL
    Returns list of {label, url, host, filename, size}
    """
    log.info(f"Fetching movie page: {page_url}")
    r = get(s, page_url)
    soup = BeautifulSoup(r.text, "lxml")

    # Scope to content area
    content = (
        soup.select_one(".entry-content")
        or soup.select_one(".post-content")
        or soup.select_one("article")
        or soup
    )

    # Find downloadwella links (and any other file host links)
    dw_links = []
    other_links = []
    seen = set()

    for a in content.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(strip=True)

        if not href or href in seen or href.startswith("javascript") or href == "#":
            continue
        if "thenkiri.com.ng" in href:  # skip internal links
            continue

        seen.add(href)

        if "downloadwella.com" in href:
            dw_links.append({"href": href, "label": text or "Download"})
        elif FILE_EXT_RE.search(href):
            other_links.append({"label": text or "Direct Download", "url": href,
                                 "host": urlparse(href).netloc.replace("www.", ""),
                                 "filename": "", "size": ""})

    results = []

    # Resolve each downloadwella link → direct file URL
    for item in dw_links:
        resolved = resolve_downloadwella(s, item["href"])
        if resolved:
            results.append({
                "label": item["label"] or resolved.get("filename", "Download"),
                "url": resolved["url"],
                "host": "downloadwella.com",
                "filename": resolved.get("filename", ""),
                "size": resolved.get("size", ""),
            })
        else:
            # Fallback: return the downloadwella page URL itself
            results.append({
                "label": item["label"] or "Download",
                "url": item["href"],
                "host": "downloadwella.com",
                "filename": "",
                "size": "",
            })

    # Append any other direct links found
    results.extend(other_links)

    return results


# ─── Endpoints ────────────────────────────────────────────────────────────────
@app.route("/api/search")
@limiter.limit("30 per minute")
def search():
    """
    GET /api/search?q=avatar
    GET /api/search?q=avatar&page=2
    Returns: { results: [{title, url, thumbnail}], count, query }
    """
    q = request.args.get("q", "").strip()
    page = request.args.get("page", "1")
    if not q or len(q) < 2:
        return jsonify({"error": "Query too short"}), 400

    url = f"{BASE}/?s={q.replace(' ', '+')}&paged={page}"
    log.info(f"SEARCH q={q!r} page={page}")

    try:
        s = session()
        r = get(s, url)
    except Exception as e:
        return jsonify({"error": "Failed to reach Nkiri", "detail": str(e)}), 502

    soup = BeautifulSoup(r.text, "lxml")
    results = parse_movie_cards(soup)
    return jsonify({"results": results, "count": len(results), "query": q, "page": page})


@app.route("/api/movies")
@limiter.limit("30 per minute")
def all_movies():
    """
    GET /api/movies                   - latest (homepage)
    GET /api/movies?page=2            - paginated
    GET /api/movies?category=hollywood  - by category slug
    GET /api/movies?category=korean-drama&page=3

    Returns: { results: [{title, url, thumbnail}], count, page, next_page }
    """
    page = request.args.get("page", "1")
    category = request.args.get("category", "").strip()

    if category:
        url = f"{BASE}/category/{category}/page/{page}/"
    else:
        url = f"{BASE}/page/{page}/" if page != "1" else BASE

    log.info(f"MOVIES category={category!r} page={page}")

    try:
        s = session()
        r = get(s, url)
    except Exception as e:
        return jsonify({"error": "Failed to reach Nkiri", "detail": str(e)}), 502

    soup = BeautifulSoup(r.text, "lxml")
    results = parse_movie_cards(soup)

    # Detect if there's a next page
    has_next = bool(soup.select_one("a.next, a[rel='next'], .nav-next a, .next.page-numbers"))
    next_page = str(int(page) + 1) if has_next else None

    # Available categories from nav (helpful for callers)
    categories = []
    for a in soup.select(".categories a, .cat-item a, nav .menu-item a"):
        href = a.get("href", "")
        m = re.search(r"/category/([^/]+)/", href)
        if m:
            categories.append({"name": a.get_text(strip=True), "slug": m.group(1)})

    return jsonify({
        "results": results,
        "count": len(results),
        "page": page,
        "next_page": next_page,
        "categories": categories,
    })


@app.route("/api/movie")
@limiter.limit("30 per minute")
def movie_info():
    """
    GET /api/movie?url=https://thenkiri.com.ng/avatar/
    Returns: { title, url, thumbnail, description, details }
    """
    page_url = request.args.get("url", "").strip()
    if not page_url:
        return jsonify({"error": "url required"}), 400
    if not is_nkiri(page_url):
        return jsonify({"error": "Only thenkiri.com.ng URLs accepted"}), 400

    log.info(f"MOVIE {page_url}")
    try:
        s = session()
        r = get(s, page_url)
    except Exception as e:
        return jsonify({"error": "Failed to fetch page", "detail": str(e)}), 502

    soup = BeautifulSoup(r.text, "lxml")
    return jsonify(parse_movie_info(soup, page_url))


@app.route("/api/download")
@limiter.limit("15 per minute")
def download():
    """
    GET /api/download?url=https://thenkiri.com.ng/avatar/

    Scrapes the Nkiri page → finds downloadwella.com links →
    resolves each one through XFilesharing POST flow →
    returns direct .mkv/.mp4 URLs that trigger browser download immediately.

    Links are NEVER cached — always live.

    Returns: { title, url, links: [{label, url, host, filename, size}], count, fetched_at }
    """
    page_url = request.args.get("url", "").strip()
    if not page_url:
        return jsonify({"error": "url required"}), 400
    if not is_nkiri(page_url):
        return jsonify({"error": "Only thenkiri.com.ng URLs accepted"}), 400

    log.info(f"DOWNLOAD {page_url}")
    try:
        s = session()
        links = resolve_download_links(s, page_url)
    except Exception as e:
        log.error(f"Download resolve error: {e}")
        return jsonify({"error": "Failed to resolve download links", "detail": str(e)}), 502

    # Get title
    title = ""
    try:
        r = get(s, page_url)
        h1 = BeautifulSoup(r.text, "lxml").find("h1")
        if h1:
            title = h1.get_text(strip=True)
    except Exception:
        pass

    return jsonify({
        "title": title,
        "url": page_url,
        "links": links,
        "count": len(links),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
