"""
Nkiri API — 3 endpoints for your movie website
================================================
GET /api/search?q=avengers          → list of movies
GET /api/movie?url=PAGE_URL         → movie info (title, poster, description, etc.)
GET /api/download?url=PAGE_URL      → fresh, live download links (never cached)
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

# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)  # allow requests from your website domain

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["120 per hour"],
    storage_uri="memory://",
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("nkiri-api")

BASE = "https://thenkiri.com.ng"

# Known file-host domains — used to identify download links
FILE_HOSTS = [
    "mediafire.com", "drive.google.com", "1fichier.com", "gofile.io",
    "pixeldrain.com", "uploadhaven.com", "racaty.net", "hexupload.net",
    "krakenfiles.com", "solidfiles.com", "send.cm", "uptobox.com",
    "rapidgator.net", "nitroflare.com", "filefox.cc", "filestore.to",
    "doodstream.com", "streamtape.com", "mixdrop.co", "filemoon.sx",
    "streamwish.com", "vidplay.online", "vidsrc.to", "embedsb.com",
    "uqload.co", "fembed.com", "berkasdrive.com", "zippyshare.com",
]

FILE_EXT_RE = re.compile(r'\.(mp4|mkv|avi|mov|webm)(\?|#|$)', re.I)


# ─── Helpers ──────────────────────────────────────────────────────────────────
def make_session():
    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    s.headers.update({"Accept-Language": "en-US,en;q=0.9", "Referer": BASE})
    return s


def is_nkiri_url(url: str) -> bool:
    return "thenkiri.com.ng" in urlparse(url).netloc


def fetch(session, url: str, timeout=25):
    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp


# ─── Scrape helpers ───────────────────────────────────────────────────────────
def parse_search_results(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    movies = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("http"):
            href = urljoin(BASE, href)

        # Skip nav/category/pagination links
        if any(x in href for x in ["/category/", "/page/", "/tag/", "/?s=", "#"]):
            continue
        if href in seen or href == BASE or href == BASE + "/":
            continue

        title = a.get_text(strip=True) or a.get("title", "")
        if not title or len(title) < 4:
            continue

        # Only pick links that look like single post/movie pages
        path_parts = urlparse(href).path.strip("/").split("/")
        if len(path_parts) != 1:  # e.g. /movie-name/  →  ['movie-name']
            continue

        seen.add(href)

        # Thumbnail — look in the parent card element
        thumb = ""
        parent = a.find_parent(["article", "div", "li"])
        if parent:
            img = parent.find("img")
            if img:
                thumb = img.get("data-src") or img.get("data-lazy-src") or img.get("src", "")

        movies.append({
            "title": title,
            "url": href,
            "thumbnail": thumb,
        })

    return movies


def parse_movie_info(html: str, page_url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    # Title
    title = ""
    for sel in ["h1.entry-title", "h1.post-title", "h1", "h2.entry-title"]:
        el = soup.select_one(sel)
        if el:
            title = el.get_text(strip=True)
            break

    # Poster / thumbnail
    thumbnail = ""
    og = soup.find("meta", property="og:image")
    if og:
        thumbnail = og.get("content", "")
    if not thumbnail:
        img = soup.select_one(".entry-content img, .post-content img, article img")
        if img:
            thumbnail = img.get("data-src") or img.get("src", "")

    # Description
    description = ""
    og_desc = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "description"})
    if og_desc:
        description = og_desc.get("content", "").strip()
    if not description:
        p = soup.select_one(".entry-content p, .post-content p")
        if p:
            description = p.get_text(strip=True)[:500]

    # Meta details (year, genre, size, etc.) — sites often put these in tables or dl/dt
    details = {}
    for row in soup.select("table tr, .post-details li, .entry-meta span"):
        text = row.get_text(" ", strip=True)
        # Key: Value patterns
        m = re.match(r"^([^:]+):\s*(.+)$", text)
        if m:
            key = m.group(1).strip().lower().replace(" ", "_")
            val = m.group(2).strip()
            if len(key) < 30 and len(val) < 200:
                details[key] = val

    return {
        "title": title,
        "url": page_url,
        "thumbnail": thumbnail,
        "description": description,
        "details": details,
    }


def parse_download_links(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    results = []
    seen = set()

    def add(url: str, label: str = "Download"):
        url = url.strip()
        if not url or url in seen or url.startswith("javascript") or url == "#":
            return
        seen.add(url)
        host = urlparse(url).netloc.replace("www.", "")
        results.append({"label": label.strip() or "Download", "url": url, "host": host})

    # 1. Anchor tags
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)

        if FILE_EXT_RE.search(href):
            add(href, text or "Direct Video")
            continue

        if any(h in href for h in FILE_HOSTS):
            add(href, text or "Download")
            continue

        if re.search(r'\b(download|get\s*file|480p|720p|1080p|4k|bluray|webrip)\b', text, re.I):
            if href.startswith("http"):
                add(href, text)

    # 2. Iframes
    for iframe in soup.find_all("iframe", src=True):
        src = iframe["src"]
        if any(h in src for h in FILE_HOSTS) or FILE_EXT_RE.search(src):
            add(src, "Embedded Player")

    # 3. JS-embedded direct URLs
    for script in soup.find_all("script"):
        text = script.string or ""
        for m in re.finditer(
            r'["\']((https?://[^"\']+?\.(?:mp4|mkv|avi|webm))[^"\']*)["\']', text, re.I
        ):
            add(m.group(1), "Direct Video")

    # 4. Meta refresh
    for meta in soup.find_all("meta", attrs={"http-equiv": re.compile("refresh", re.I)}):
        m = re.search(r"url=([^\s;]+)", meta.get("content", ""), re.I)
        if m:
            add(m.group(1).strip("'\""), "Redirect")

    return results


# ─── API Endpoints ─────────────────────────────────────────────────────────────

@app.route("/api/search")
@limiter.limit("30 per minute")
def search():
    """
    Search for movies.

    GET /api/search?q=avengers
    GET /api/search?q=avengers&page=2

    Response:
    {
      "results": [
        { "title": "...", "url": "https://thenkiri.com.ng/...", "thumbnail": "..." },
        ...
      ],
      "count": 12
    }
    """
    q = request.args.get("q", "").strip()
    page = request.args.get("page", "1")

    if not q or len(q) < 2:
        return jsonify({"error": "Query must be at least 2 characters"}), 400

    search_url = f"{BASE}/?s={q.replace(' ', '+')}&paged={page}"
    log.info(f"SEARCH  q={q!r}  page={page}")

    try:
        session = make_session()
        resp = fetch(session, search_url)
    except Exception as e:
        log.error(f"Search fetch failed: {e}")
        return jsonify({"error": "Failed to reach Nkiri", "detail": str(e)}), 502

    results = parse_search_results(resp.text)

    return jsonify({"results": results, "count": len(results), "query": q})


@app.route("/api/movie")
@limiter.limit("30 per minute")
def movie_info():
    """
    Get detailed info for a single movie page.
    Call this when a user clicks on a movie card.

    GET /api/movie?url=https://thenkiri.com.ng/movie-name/

    Response:
    {
      "title": "Movie Title",
      "url": "...",
      "thumbnail": "...",
      "description": "...",
      "details": { "year": "2023", "genre": "Action", ... }
    }
    """
    page_url = request.args.get("url", "").strip()
    if not page_url:
        return jsonify({"error": "url parameter is required"}), 400
    if not is_nkiri_url(page_url):
        return jsonify({"error": "Only thenkiri.com.ng URLs are accepted"}), 400

    log.info(f"MOVIE   url={page_url}")

    try:
        session = make_session()
        resp = fetch(session, page_url)
    except Exception as e:
        log.error(f"Movie fetch failed: {e}")
        return jsonify({"error": "Failed to fetch movie page", "detail": str(e)}), 502

    info = parse_movie_info(resp.text, page_url)
    return jsonify(info)


@app.route("/api/download")
@limiter.limit("20 per minute")
def download_links():
    """
    Fetch FRESH, live download links for a movie page.
    Always scraped in real-time — never cached — so links never expire.
    Call this ONLY when a user clicks the Download button.

    GET /api/download?url=https://thenkiri.com.ng/movie-name/

    Response:
    {
      "title": "Movie Title",
      "url": "...",
      "links": [
        { "label": "Download 720p", "url": "https://mediafire.com/...", "host": "mediafire.com" },
        { "label": "Download 1080p", "url": "https://...", "host": "..." }
      ],
      "count": 2,
      "fetched_at": "2025-05-22T10:00:00Z"
    }
    """
    page_url = request.args.get("url", "").strip()
    if not page_url:
        return jsonify({"error": "url parameter is required"}), 400
    if not is_nkiri_url(page_url):
        return jsonify({"error": "Only thenkiri.com.ng URLs are accepted"}), 400

    log.info(f"DOWNLOAD url={page_url}")

    try:
        session = make_session()
        resp = fetch(session, page_url)
    except Exception as e:
        log.error(f"Download fetch failed: {e}")
        return jsonify({"error": "Failed to fetch movie page", "detail": str(e)}), 502

    links = parse_download_links(resp.text)

    # Grab title for convenience
    soup = BeautifulSoup(resp.text, "lxml")
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""

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


# ─── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=True, port=5000)
