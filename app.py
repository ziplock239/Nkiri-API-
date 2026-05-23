"""
Nkiri API
=========
GET /api/movies                          - latest movies (paginated)
GET /api/movies?page=2                   - next page
GET /api/movies?category=hollywood       - by category
GET /api/search?q=avatar                 - search
GET /api/movie?url=PAGE_URL              - single movie info
GET /api/download?url=PAGE_URL          - fresh direct download links
GET /api/health                          - health check
"""

import re
import time
import logging
import zlib
import brotli
from urllib.parse import urljoin, urlparse

import requests
import cloudscraper
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# ─── App setup ────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)
limiter = Limiter(get_remote_address, app=app, default_limits=["300 per hour"], storage_uri="memory://")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("nkiri")

BASE = "https://thenkiri.com.ng"
FILE_EXT_RE = re.compile(r'\.(mp4|mkv|avi|mov|webm)(\?|#|$)', re.I)

# ─── Session factory ──────────────────────────────────────────────────────────
def make_session():
    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False},
        delay=3,
    )
    # Accept-Encoding: identity forces plain text — no gzip/brotli garbling
    s.headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",   # ← KEY: disables compression so we get plain HTML
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "no-cache",
    })
    return s

def fetch(s, url, timeout=30):
    s.headers["Referer"] = BASE
    r = s.get(url, timeout=timeout)
    r.raise_for_status()
    return r

def fetch_post(s, url, data, referer, timeout=30):
    s.headers.update({
        "Referer": referer,
        "Origin": urlparse(url).scheme + "://" + urlparse(url).netloc,
        "Content-Type": "application/x-www-form-urlencoded",
    })
    r = s.post(url, data=data, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return r

def is_nkiri(url):
    return "thenkiri.com.ng" in urlparse(url).netloc

# ─── Movie card parser ────────────────────────────────────────────────────────
def parse_movie_cards(soup) -> list[dict]:
    movies = []
    seen = set()

    for article in soup.select("article"):
        # Get the permalink anchor
        a = (
            article.select_one("a[rel='bookmark']")
            or article.select_one(".entry-title a")
            or article.select_one("h2 a")
            or article.select_one("h3 a")
            or article.find("a", href=True)
        )
        if not a:
            continue

        href = a.get("href", "")
        if not href.startswith("http"):
            href = urljoin(BASE, href)
        if href in seen:
            continue
        if any(x in href for x in ["/category/", "/tag/", "/page/", "/?s=", "#"]):
            continue
        # Must be a single-slug post URL e.g. /movie-name/
        path_parts = [p for p in urlparse(href).path.strip("/").split("/") if p]
        if len(path_parts) != 1:
            continue

        seen.add(href)

        # Title — prefer entry-title, fall back to anchor text
        title_el = article.select_one(".entry-title")
        title = (title_el or a).get_text(strip=True)

        # Thumbnail
        thumb = ""
        img = article.select_one("img")
        if img:
            thumb = (img.get("data-src") or img.get("data-lazy-src") or img.get("src", "")).strip()

        movies.append({"title": title, "url": href, "thumbnail": thumb})

    return movies


# ─── Movie info parser ────────────────────────────────────────────────────────
def parse_movie_info(soup, page_url: str) -> dict:
    # Title
    title = ""
    for sel in ["h1.entry-title", "h1.post-title", "h1"]:
        el = soup.select_one(sel)
        if el:
            title = el.get_text(strip=True)
            break

    # Thumbnail
    thumbnail = ""
    og = soup.find("meta", property="og:image")
    if og:
        thumbnail = og.get("content", "")
    if not thumbnail:
        img = soup.select_one(".entry-content img, .post-thumbnail img, article img")
        if img:
            thumbnail = (img.get("data-src") or img.get("src", "")).strip()

    # Description
    description = ""
    for meta_sel in [
        {"property": "og:description"},
        {"name": "description"},
    ]:
        m = soup.find("meta", attrs=meta_sel)
        if m and m.get("content"):
            description = m["content"].strip()
            break

    # Details — parse table rows and bold-colon patterns
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

    return {
        "title": title,
        "url": page_url,
        "thumbnail": thumbnail,
        "description": description,
        "details": details,
    }


# ─── Downloadwella direct-link resolver ──────────────────────────────────────
def resolve_downloadwella(s, dw_url: str) -> str | None:
    """
    Downloadwella (XFilesharing Pro) flow — confirmed from debug output:

    The page already serves a form with op=download2 pre-filled.
    Just POST it directly → follow redirect → get direct file URL.

    Form fields confirmed: op, id, rand, referer, method_free, method_premium
    """
    log.info(f"    Resolving downloadwella: {dw_url}")
    host_base = urlparse(dw_url).scheme + "://" + urlparse(dw_url).netloc

    try:
        # GET the downloadwella page
        r1 = fetch(s, dw_url)
        soup = BeautifulSoup(r1.text, "lxml")

        # Find the form — confirmed it has op=download2
        form = soup.find("form")
        if not form:
            log.warning("    No form on downloadwella page")
            return None

        # Collect all hidden inputs exactly as they are
        data = {}
        for inp in form.find_all("input"):
            name = inp.get("name", "")
            value = inp.get("value", "")
            if name:
                data[name] = value

        # Ensure we post as free download
        data["op"] = "download2"
        data["method_free"] = "Free Download"
        data.pop("method_premium", None)

        form_action = form.get("action", "").strip()
        post_url = urljoin(host_base, form_action) if form_action else dw_url

        log.info(f"    POST {post_url}  data={data}")

        # POST → should redirect to direct file URL
        r2 = fetch_post(s, post_url, data=data, referer=dw_url)

        # Check if we were redirected to a direct file URL
        if r2.history:
            for resp in list(r2.history) + [r2]:
                if FILE_EXT_RE.search(resp.url):
                    log.info(f"    Resolved via redirect: {resp.url}")
                    return resp.url

        # Check final URL
        if FILE_EXT_RE.search(r2.url):
            log.info(f"    Resolved final URL: {r2.url}")
            return r2.url

        # Scan response HTML for direct link
        soup2 = BeautifulSoup(r2.text, "lxml")

        for a in soup2.find_all("a", href=True):
            href = a["href"]
            if FILE_EXT_RE.search(href) and href.startswith("http"):
                log.info(f"    Resolved from anchor: {href}")
                return href

        for script in soup2.find_all("script"):
            t = script.string or ""
            m = re.search(r'["\']((https?://[^"\']{10,}\.(?:mp4|mkv|avi|webm))[^"\']*)["\']', t, re.I)
            if m:
                log.info(f"    Resolved from JS: {m.group(1)}")
                return m.group(1)

        # Check Location header
        loc = r2.headers.get("Location", "")
        if loc and FILE_EXT_RE.search(loc):
            return loc

        # Last resort: look for any download link on response page
        for a in soup2.find_all("a", href=True):
            href = a["href"]
            if href.startswith("http") and "downloadwella.com" not in href and "thenkiri" not in href:
                text = a.get_text(strip=True).lower()
                if "download" in text or FILE_EXT_RE.search(href):
                    log.info(f"    Resolved from fallback anchor: {href}")
                    return href

        log.warning(f"    Could not resolve direct link. Response URL: {r2.url}")
        log.warning(f"    Response snippet: {r2.text[:500]}")
        return None

    except Exception as e:
        log.error(f"    downloadwella error: {e}")
        return None


# ─── Main download link collector ────────────────────────────────────────────
def get_download_links(s, nkiri_page_url: str) -> list[dict]:
    """
    1. Fetch the Nkiri movie page
    2. Find all downloadwella.com links in the content area
    3. Resolve each one to a direct file URL via POST
    """
    r = fetch(s, nkiri_page_url)
    soup = BeautifulSoup(r.text, "lxml")

    content = (
        soup.select_one(".entry-content")
        or soup.select_one(".post-content")
        or soup.select_one("article")
        or soup
    )

    dw_links = []
    direct_links = []
    seen = set()

    for a in content.find_all("a", href=True):
        href = a["href"].strip()
        label = a.get_text(strip=True)

        if not href or href in seen:
            continue
        if href.startswith("javascript") or href == "#":
            continue
        if "thenkiri.com.ng" in href:
            continue

        seen.add(href)

        if "downloadwella.com" in href:
            dw_links.append({"href": href, "label": label})
        elif FILE_EXT_RE.search(href) and href.startswith("http"):
            direct_links.append({
                "label": label or "Direct Download",
                "url": href,
                "host": urlparse(href).netloc.replace("www.", ""),
                "filename": href.split("/")[-1].split("?")[0],
                "size": "",
            })

    results = []

    for item in dw_links:
        direct_url = resolve_downloadwella(s, item["href"])
        if direct_url:
            filename = direct_url.split("/")[-1].split("?")[0]
            results.append({
                "label": item["label"] or filename or "Download",
                "url": direct_url,
                "host": urlparse(direct_url).netloc.replace("www.", ""),
                "filename": filename,
                "size": "",
            })
        else:
            # Couldn't resolve — return the downloadwella page URL as fallback
            results.append({
                "label": item["label"] or "Download",
                "url": item["href"],
                "host": "downloadwella.com",
                "filename": "",
                "size": "",
                "note": "could not resolve to direct link",
            })

    results.extend(direct_links)
    return results


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.route("/api/movies")
@limiter.limit("30 per minute")
def all_movies():
    """
    GET /api/movies
    GET /api/movies?page=2
    GET /api/movies?category=hollywood
    GET /api/movies?category=korean-drama&page=3

    Response: { results:[{title,url,thumbnail}], count, page, next_page, categories }
    """
    page     = request.args.get("page", "1").strip()
    category = request.args.get("category", "").strip()

    if category:
        url = f"{BASE}/category/{category}/page/{page}/" if page != "1" else f"{BASE}/category/{category}/"
    else:
        url = f"{BASE}/page/{page}/" if page != "1" else BASE

    log.info(f"MOVIES  category={category!r}  page={page}  → {url}")

    try:
        s = make_session()
        r = fetch(s, url)
    except Exception as e:
        return jsonify({"error": "Failed to reach Nkiri", "detail": str(e)}), 502

    soup = BeautifulSoup(r.text, "lxml")
    results = parse_movie_cards(soup)

    has_next = bool(soup.select_one("a.next, a[rel='next'], .nav-next a, .next.page-numbers"))
    next_page = str(int(page) + 1) if has_next else None

    cats = []
    seen_slugs = set()
    for a in soup.select(".cat-item a, .categories a, nav .menu-item a"):
        href = a.get("href", "")
        m = re.search(r"/category/([^/]+)/?", href)
        if m and m.group(1) not in seen_slugs:
            seen_slugs.add(m.group(1))
            cats.append({"name": a.get_text(strip=True), "slug": m.group(1)})

    return jsonify({
        "results":    results,
        "count":      len(results),
        "page":       page,
        "next_page":  next_page,
        "categories": cats,
    })


@app.route("/api/search")
@limiter.limit("30 per minute")
def search():
    """
    GET /api/search?q=avatar
    GET /api/search?q=avatar&page=2

    Response: { results:[{title,url,thumbnail}], count, query, page }
    """
    q    = request.args.get("q", "").strip()
    page = request.args.get("page", "1").strip()

    if not q or len(q) < 2:
        return jsonify({"error": "Query too short"}), 400

    url = f"{BASE}/?s={q.replace(' ', '+')}&paged={page}"
    log.info(f"SEARCH  q={q!r}  page={page}")

    try:
        s = make_session()
        r = fetch(s, url)
    except Exception as e:
        return jsonify({"error": "Failed to reach Nkiri", "detail": str(e)}), 502

    soup = BeautifulSoup(r.text, "lxml")
    results = parse_movie_cards(soup)

    return jsonify({"results": results, "count": len(results), "query": q, "page": page})


@app.route("/api/movie")
@limiter.limit("30 per minute")
def movie_info():
    """
    GET /api/movie?url=https://thenkiri.com.ng/avatar/

    Response: { title, url, thumbnail, description, details }
    """
    page_url = request.args.get("url", "").strip()
    if not page_url:
        return jsonify({"error": "url required"}), 400
    if not is_nkiri(page_url):
        return jsonify({"error": "Only thenkiri.com.ng URLs accepted"}), 400

    log.info(f"MOVIE   {page_url}")
    try:
        s = make_session()
        r = fetch(s, page_url)
    except Exception as e:
        return jsonify({"error": "Failed to fetch page", "detail": str(e)}), 502

    soup = BeautifulSoup(r.text, "lxml")
    return jsonify(parse_movie_info(soup, page_url))


@app.route("/api/download")
@limiter.limit("15 per minute")
def download():
    """
    GET /api/download?url=https://thenkiri.com.ng/avatar/

    Fetches the Nkiri page → finds downloadwella links → POSTs through
    XFilesharing to get the direct .mkv/.mp4 URL → returns it.
    Always live, never cached.

    Response: { title, url, links:[{label,url,host,filename,size}], count, fetched_at }
    """
    page_url = request.args.get("url", "").strip()
    if not page_url:
        return jsonify({"error": "url required"}), 400
    if not is_nkiri(page_url):
        return jsonify({"error": "Only thenkiri.com.ng URLs accepted"}), 400

    log.info(f"DOWNLOAD {page_url}")
    try:
        s = make_session()
        links = get_download_links(s, page_url)

        # Also grab title while we have the session
        r = fetch(s, page_url)
        soup = BeautifulSoup(r.text, "lxml")
        info = parse_movie_info(soup, page_url)
        title = info.get("title", "")

    except Exception as e:
        log.error(f"Download error: {e}")
        return jsonify({"error": "Failed to resolve download links", "detail": str(e)}), 502

    return jsonify({
        "title":      title,
        "url":        page_url,
        "links":      links,
        "count":      len(links),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/debug")
def debug():
    """Temporary — dumps raw HTML so you can inspect what the server sees."""
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "url required"}), 400
    try:
        s = make_session()
        r = s.get(url, timeout=30)
        soup = BeautifulSoup(r.text, "lxml")

        links = [{"text": a.get_text(strip=True)[:80], "href": a["href"][:200]}
                 for a in soup.find_all("a", href=True)]
        forms = []
        for form in soup.find_all("form"):
            inputs = [{"type": i.get("type"), "name": i.get("name"), "value": (i.get("value") or "")[:100]}
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

        return jsonify({
            "status_code": r.status_code,
            "final_url": r.url,
            "html_length": len(r.text),
            "html_snippet": r.text[:4000],
            "articles_found": len(soup.find_all("article")),
            "articles_sample": articles,
            "entry_content_found": bool(soup.select_one(".entry-content")),
            "all_links": links[:60],
            "all_forms": forms,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
