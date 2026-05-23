"""
Nkiri API
=========
GET /api/movies                    - latest movies (paginated)
GET /api/movies?page=2
GET /api/movies?category=k-drama
GET /api/search?q=avatar
GET /api/movie?url=PAGE_URL
GET /api/download?url=PAGE_URL
GET /api/health
GET /api/debug?url=...
"""

import re
import time
import gzip
import zlib
import logging
from urllib.parse import urljoin, urlparse, unquote

import cloudscraper
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

try:
    import brotli
    HAS_BROTLI = True
except ImportError:
    HAS_BROTLI = False

app = Flask(__name__)
CORS(app)
limiter = Limiter(get_remote_address, app=app, default_limits=["300 per hour"], storage_uri="memory://")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("nkiri")

BASE = "https://thenkiri.com.ng"
FILE_EXT_RE = re.compile(r'\.(mp4|mkv|avi|mov|webm)(\?|#|$)', re.I)

# ─── Session + decode ─────────────────────────────────────────────────────────
def make_session():
    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False},
        delay=3,
    )
    s.headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "no-cache",
    })
    return s


def decode_body(r) -> str:
    raw = r.content
    if HAS_BROTLI:
        try:
            return brotli.decompress(raw).decode("utf-8", errors="replace")
        except Exception:
            pass
    try:
        return gzip.decompress(raw).decode("utf-8", errors="replace")
    except Exception:
        pass
    for wbits in (15, -15, 47):
        try:
            return zlib.decompress(raw, wbits).decode("utf-8", errors="replace")
        except Exception:
            pass
    return r.text


def nkiri_get(s, url, timeout=30):
    s.headers["Referer"] = BASE
    r = s.get(url, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return r, decode_body(r)


def dw_post(s, url, data, referer, timeout=30):
    s.headers.update({
        "Referer": referer,
        "Origin": urlparse(url).scheme + "://" + urlparse(url).netloc,
        "Content-Type": "application/x-www-form-urlencoded",
    })
    r = s.post(url, data=data, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return r, decode_body(r)


def is_nkiri(url):
    return "thenkiri.com.ng" in urlparse(url).netloc


# ─── Movie card parser ────────────────────────────────────────────────────────
# Confirmed from debug: movies are plain <a> links, no <article> on homepage.
# Movie page HAS articles (post-XXXX class) but first link is category, not movie.
# Safest: match by URL slug pattern confirmed from real debug data.

MOVIE_URL_RE = re.compile(
    r'https://thenkiri\.com\.ng/([a-z0-9][a-z0-9-]+)/$',
    re.I
)

# Slugs to exclude even if they match the URL pattern
EXCLUDE_SLUGS = {
    "login-secure", "wp-login", "wp-admin", "feed", "sitemap",
    "contact", "about", "privacy-policy", "terms",
}

# Keywords that confirm a slug is a movie/show post
CONTENT_KEYWORDS = re.compile(
    r'\b(download|korean-drama|tv-series|chinese-drama|thai-drama|k-drama|'
    r'hollywood|nollywood|bollywood|foreign|anime|movie|drama|series|'
    r's\d{2}|episode)\b',
    re.I
)


def parse_movie_cards(soup) -> list[dict]:
    movies = []
    seen_urls = set()

    # Build a map of url → thumbnail from ALL images on the page
    # Nkiri wraps each post card in a div that contains both the <a> and <img>
    thumb_map = {}
    for img in soup.find_all("img"):
        src = (img.get("data-src") or img.get("data-lazy-src") or img.get("src", "")).strip()
        if not src or src.startswith("data:") or len(src) < 20:
            continue
        # Walk up to find the nearest ancestor that also has a link
        parent = img.find_parent(["div", "li", "article", "figure"])
        if parent:
            for a in parent.find_all("a", href=True):
                href = a["href"].strip()
                if href and href not in thumb_map:
                    thumb_map[href] = src

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(strip=True)

        if not href or href in seen_urls:
            continue
        if not text:
            continue

        m = MOVIE_URL_RE.match(href)
        if not m:
            continue

        slug = m.group(1)
        if slug in EXCLUDE_SLUGS:
            continue
        if not CONTENT_KEYWORDS.search(slug):
            continue

        seen_urls.add(href)
        thumb = thumb_map.get(href, "")
        movies.append({"title": text, "url": href, "thumbnail": thumb})

    return movies


# ─── Movie info parser ────────────────────────────────────────────────────────
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
        # Use the post's own featured image (first article img)
        art = soup.select_one("article")
        if art:
            img = art.find("img")
            if img:
                thumbnail = (img.get("data-src") or img.get("src", "")).strip()

    description = ""
    for attr in [{"property": "og:description"}, {"name": "description"}]:
        m = soup.find("meta", attrs=attr)
        if m and m.get("content"):
            description = m["content"].strip()
            break

    details = {}
    content = soup.select_one(".entry-content, .post-content")
    if content:
        for row in content.select("table tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) >= 2:
                k = cells[0].get_text(strip=True).lower().replace(" ", "_").rstrip(":")
                v = cells[1].get_text(strip=True)
                if k and v and len(k) < 40:
                    details[k] = v
        for p in content.select("p, li"):
            text = p.get_text(" ", strip=True)
            m = re.match(r"^([A-Za-z][A-Za-z ]{1,24}):\s*(.+)$", text)
            if m:
                k = m.group(1).strip().lower().replace(" ", "_")
                if k not in details:
                    details[k] = m.group(2).strip()[:300]

    return {"title": title, "url": page_url, "thumbnail": thumbnail,
            "description": description, "details": details}


# ─── Download link extractor ──────────────────────────────────────────────────
def extract_dw_urls_from_html(html: str) -> list[str]:
    """
    Find ALL downloadwella.com URLs in raw HTML — including those buried
    in JavaScript variables, onclick handlers, data attributes, etc.
    Nkiri loads download links via JS so they won't appear as plain <a> tags.
    """
    # Match any downloadwella.com URL in the entire raw HTML
    pattern = re.compile(
        r'https?://(?:www\.)?downloadwella\.com/[^\s\'"<>\]\\]+',
        re.I
    )
    found = []
    seen = set()
    for m in pattern.finditer(html):
        url = m.group(0).rstrip(".,;)")  # strip trailing punctuation
        # Only keep file page URLs (not homepage/nav)
        if re.search(r'/[a-z0-9]{8,}/', url, re.I):  # has file ID segment
            if url not in seen:
                seen.add(url)
                found.append(url)
    return found


def extract_label_for_dw_url(url: str, html: str) -> str:
    """Try to find a label near this URL in the raw HTML."""
    idx = html.find(url)
    if idx == -1:
        return "Download"
    # Look at surrounding 300 chars for quality keywords
    context = html[max(0, idx-200):idx+200]
    for q in ["2160p", "4K", "1080p", "720p", "480p", "360p", "BluRay", "WEBRip", "HDCAM"]:
        if q.lower() in context.lower():
            return f"Download {q}"
    return "Download"


# ─── Downloadwella resolver ───────────────────────────────────────────────────
def resolve_downloadwella(s, dw_url: str) -> str | None:
    log.info(f"  dw resolve: {dw_url}")
    host_base = urlparse(dw_url).scheme + "://" + urlparse(dw_url).netloc

    try:
        r1, body1 = nkiri_get(s, dw_url)
        soup1 = BeautifulSoup(body1, "lxml")

        form = soup1.find("form")
        if not form:
            log.warning("  no form on dw page")
            return None

        data = {}
        for inp in form.find_all("input"):
            name = inp.get("name", "")
            val = inp.get("value", "")
            if name:
                data[name] = val

        data["op"] = "download2"
        data["method_free"] = "Free Download"
        data.pop("method_premium", None)

        action = form.get("action", "").strip()
        post_url = urljoin(host_base, action) if action else dw_url

        log.info(f"  POST {post_url} data={data}")
        r2, body2 = dw_post(s, post_url, data, referer=dw_url)

        # Check redirect chain
        for resp in list(r2.history) + [r2]:
            if FILE_EXT_RE.search(resp.url):
                log.info(f"  resolved via redirect: {resp.url}")
                return resp.url

        # Scan response for direct file link
        soup2 = BeautifulSoup(body2, "lxml")
        for a in soup2.find_all("a", href=True):
            if FILE_EXT_RE.search(a["href"]) and a["href"].startswith("http"):
                return a["href"]

        for script in soup2.find_all("script"):
            t = script.string or ""
            m = re.search(r'["\']((https?://[^"\']{10,}\.(?:mp4|mkv|avi|webm))[^"\']*)["\']', t, re.I)
            if m:
                return m.group(1)

        # Also scan raw body2 for file URLs
        m = re.search(r'https?://[^\s"\'<>]+\.(?:mp4|mkv|avi|webm)[^\s"\'<>]*', body2, re.I)
        if m:
            return m.group(0)

        log.warning(f"  dw could not resolve. body snippet: {body2[:400]}")
        return None

    except Exception as e:
        log.error(f"  dw error: {e}")
        return None


# ─── Main download collector ──────────────────────────────────────────────────
def get_download_links(s, nkiri_url: str):
    r, body = nkiri_get(s, nkiri_url)
    soup = BeautifulSoup(body, "lxml")
    info = parse_movie_info(soup, nkiri_url)
    title = info.get("title", "")

    # Search the ENTIRE raw HTML for downloadwella URLs
    # (they are injected via JS so won't appear as plain anchor tags)
    dw_urls = extract_dw_urls_from_html(body)
    log.info(f"  found {len(dw_urls)} downloadwella URLs in raw HTML")

    results = []
    for dw_url in dw_urls:
        label = extract_label_for_dw_url(dw_url, body)
        direct_url = resolve_downloadwella(s, dw_url)
        if direct_url:
            filename = unquote(direct_url.split("/")[-1].split("?")[0])
            results.append({
                "label": label,
                "url": direct_url,
                "host": urlparse(direct_url).netloc.replace("www.", ""),
                "filename": filename,
                "size": "",
            })
        else:
            results.append({
                "label": label,
                "url": dw_url,
                "host": "downloadwella.com",
                "filename": "",
                "size": "",
                "note": "direct link resolution failed",
            })

    # Also catch any plain direct links in content area
    content = soup.select_one(".entry-content, .post-content") or soup
    for a in content.find_all("a", href=True):
        href = a["href"].strip()
        if FILE_EXT_RE.search(href) and href.startswith("http"):
            if "thenkiri" not in href:
                results.append({
                    "label": a.get_text(strip=True) or "Direct Download",
                    "url": href,
                    "host": urlparse(href).netloc.replace("www.", ""),
                    "filename": href.split("/")[-1].split("?")[0],
                    "size": "",
                })

    return title, results


# ─── Endpoints ────────────────────────────────────────────────────────────────
@app.route("/api/movies")
@limiter.limit("30 per minute")
def all_movies():
    page = request.args.get("page", "1").strip()
    category = request.args.get("category", "").strip()

    if category:
        url = f"{BASE}/category/{category}/" if page == "1" else f"{BASE}/category/{category}/page/{page}/"
    else:
        url = BASE if page == "1" else f"{BASE}/page/{page}/"

    log.info(f"MOVIES category={category!r} page={page}")
    try:
        s = make_session()
        _, body = nkiri_get(s, url)
    except Exception as e:
        return jsonify({"error": "Failed to reach Nkiri", "detail": str(e)}), 502

    soup = BeautifulSoup(body, "lxml")
    results = parse_movie_cards(soup)
    has_next = bool(soup.select_one("a.next, a[rel='next'], .nav-next a, .next.page-numbers"))
    next_page = str(int(page) + 1) if has_next else None

    cats, seen_slugs = [], set()
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        m = re.search(r"/category/([^/]+)/?$", href)
        if m and m.group(1) not in seen_slugs and "uncategorized" not in m.group(1):
            seen_slugs.add(m.group(1))
            cats.append({"name": a.get_text(strip=True), "slug": m.group(1)})

    return jsonify({"results": results, "count": len(results),
                    "page": page, "next_page": next_page, "categories": cats})


@app.route("/api/search")
@limiter.limit("30 per minute")
def search():
    q = request.args.get("q", "").strip()
    page = request.args.get("page", "1").strip()
    if not q or len(q) < 2:
        return jsonify({"error": "Query too short"}), 400

    url = f"{BASE}/?s={q.replace(' ', '+')}&paged={page}"
    log.info(f"SEARCH q={q!r} page={page}")
    try:
        s = make_session()
        _, body = nkiri_get(s, url)
    except Exception as e:
        return jsonify({"error": "Failed to reach Nkiri", "detail": str(e)}), 502

    soup = BeautifulSoup(body, "lxml")
    results = parse_movie_cards(soup)
    return jsonify({"results": results, "count": len(results), "query": q, "page": page})


@app.route("/api/movie")
@limiter.limit("30 per minute")
def movie_info():
    page_url = request.args.get("url", "").strip()
    if not page_url:
        return jsonify({"error": "url required"}), 400
    if not is_nkiri(page_url):
        return jsonify({"error": "Only thenkiri.com.ng URLs accepted"}), 400

    log.info(f"MOVIE {page_url}")
    try:
        s = make_session()
        _, body = nkiri_get(s, page_url)
    except Exception as e:
        return jsonify({"error": "Failed to fetch page", "detail": str(e)}), 502

    soup = BeautifulSoup(body, "lxml")
    return jsonify(parse_movie_info(soup, page_url))


@app.route("/api/download")
@limiter.limit("15 per minute")
def download():
    page_url = request.args.get("url", "").strip()
    if not page_url:
        return jsonify({"error": "url required"}), 400
    if not is_nkiri(page_url):
        return jsonify({"error": "Only thenkiri.com.ng URLs accepted"}), 400

    log.info(f"DOWNLOAD {page_url}")
    try:
        s = make_session()
        title, links = get_download_links(s, page_url)
    except Exception as e:
        log.error(f"Download error: {e}")
        return jsonify({"error": "Failed to resolve links", "detail": str(e)}), 502

    return jsonify({
        "title": title, "url": page_url,
        "links": links, "count": len(links),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/debug")
def debug():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "url required"}), 400
    try:
        s = make_session()
        r, body = nkiri_get(s, url)
        soup = BeautifulSoup(body, "lxml")

        links = [{"text": a.get_text(strip=True)[:80], "href": a["href"][:200]}
                 for a in soup.find_all("a", href=True)]
        forms = []
        for form in soup.find_all("form"):
            inputs = [{"type": i.get("type"), "name": i.get("name"),
                       "value": (i.get("value") or "")[:100]}
                      for i in form.find_all("input")]
            forms.append({"action": form.get("action"), "method": form.get("method"), "inputs": inputs})

        articles = []
        for art in soup.find_all("article")[:5]:
            a = art.find("a", href=True)
            img = art.find("img")
            articles.append({
                "classes": art.get("class", []),
                "link": a["href"] if a else None,
                "link_text": a.get_text(strip=True)[:80] if a else None,
                "img": (img.get("data-src") or img.get("src", ""))[:200] if img else None,
            })

        # Show downloadwella URLs found in raw HTML
        dw_urls = extract_dw_urls_from_html(body)

        return jsonify({
            "status_code": r.status_code,
            "content_encoding": r.headers.get("Content-Encoding", "none"),
            "final_url": r.url,
            "raw_bytes_length": len(r.content),
            "decoded_html_length": len(body),
            "html_snippet": body[:3000],
            "articles_found": len(soup.find_all("article")),
            "articles_sample": articles,
            "entry_content_found": bool(soup.select_one(".entry-content")),
            "all_links_count": len(links),
            "all_links": links[:80],
            "all_forms": forms,
            "downloadwella_urls_in_raw_html": dw_urls,  # ← KEY: shows JS-loaded DW links
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
