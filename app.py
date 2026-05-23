"""
Nkiri API
=========
GET /api/movies?page=1&category=k-drama
GET /api/search?q=avatar
GET /api/movie?url=PAGE_URL
GET /api/download?url=PAGE_URL
GET /api/debug?url=PAGE_URL
GET /api/health
"""

import re, time, gzip, zlib, logging, base64
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


# ── HTTP helpers ──────────────────────────────────────────────────────────────
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


def decode_body(r):
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
    for wb in (15, -15, 47):
        try:
            return zlib.decompress(raw, wb).decode("utf-8", errors="replace")
        except Exception:
            pass
    return r.text


def fetch(s, url, referer=None, timeout=30):
    s.headers["Referer"] = referer or BASE
    r = s.get(url, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return r, decode_body(r)


def post_form(s, url, data, referer, timeout=30):
    origin = urlparse(url).scheme + "://" + urlparse(url).netloc
    s.headers.update({
        "Referer": referer,
        "Origin": origin,
        "Content-Type": "application/x-www-form-urlencoded",
    })
    r = s.post(url, data=data, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return r, decode_body(r)


def is_nkiri(url):
    return "thenkiri.com.ng" in urlparse(url).netloc


# ── Movie card parser ─────────────────────────────────────────────────────────
MOVIE_URL_RE = re.compile(r'https://thenkiri\.com\.ng/([a-z0-9][a-z0-9-]+)/$', re.I)
CONTENT_KW = re.compile(
    r'\b(download|korean-drama|tv-series|chinese-drama|thai-drama|k-drama|'
    r'hollywood|nollywood|bollywood|foreign|anime|movie|drama|series|s\d{2}|'
    r'episode|documentary)\b', re.I
)
SKIP_SLUGS = {
    "login-secure", "wp-login", "wp-admin", "feed",
    "sitemap", "contact", "about", "privacy-policy", "terms",
}


def parse_movie_cards(soup):
    thumb_map = {}
    for img in soup.find_all("img"):
        src = (img.get("data-src") or img.get("data-lazy-src") or img.get("src", "")).strip()
        if not src or src.startswith("data:") or len(src) < 20:
            continue
        parent = img.find_parent(["div", "li", "article", "figure"])
        if parent:
            for a in parent.find_all("a", href=True):
                h = a["href"].strip()
                if h and h not in thumb_map:
                    thumb_map[h] = src

    movies, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(strip=True)
        if not href or not text or href in seen:
            continue
        m = MOVIE_URL_RE.match(href)
        if not m:
            continue
        slug = m.group(1)
        if slug in SKIP_SLUGS or not CONTENT_KW.search(slug):
            continue
        seen.add(href)
        movies.append({"title": text, "url": href, "thumbnail": thumb_map.get(href, "")})
    return movies


# ── Movie info parser ─────────────────────────────────────────────────────────
def parse_movie_info(soup, page_url):
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
            t = p.get_text(" ", strip=True)
            m = re.match(r"^([A-Za-z][A-Za-z ]{1,24}):\s*(.+)$", t)
            if m:
                k = m.group(1).strip().lower().replace(" ", "_")
                if k not in details:
                    details[k] = m.group(2).strip()[:300]

    return {
        "title": title, "url": page_url,
        "thumbnail": thumbnail, "description": description, "details": details,
    }


# ── Find omg10 links in Nkiri page ────────────────────────────────────────────
def find_omg10_links(soup, body):
    links, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if "omg10.com" not in href or href in seen:
            continue
        seen.add(href)
        label = a.get_text(strip=True)
        idx = body.find(href)
        if idx != -1:
            ctx = body[max(0, idx - 300):idx + 300]
            for q in ["2160p", "4K", "1080p", "720p", "480p", "BluRay", "WEBRip", "WEB-DL"]:
                if q.lower() in ctx.lower():
                    label = label or f"Download {q}"
                    break
        links.append({"url": href, "label": label or "Download"})

    # Also scan raw body for omg10 URLs not in anchor tags
    for m in re.finditer(r'https?://(?:www\.)?omg10\.com/\d+/\d+', body):
        url = m.group(0)
        if url not in seen:
            seen.add(url)
            links.append({"url": url, "label": "Download"})

    return links


# ── Resolve omg10 → downloadwella ─────────────────────────────────────────────
def resolve_omg10(s, omg_url):
    log.info(f"  omg10: {omg_url}")
    try:
        r, body = fetch(s, omg_url, referer=BASE)
        soup = BeautifulSoup(body, "lxml")

        # Direct anchor to downloadwella
        for a in soup.find_all("a", href=True):
            if "downloadwella.com" in a["href"]:
                return a["href"]

        # Meta refresh
        for meta in soup.find_all("meta", attrs={"http-equiv": re.compile("refresh", re.I)}):
            content = meta.get("content", "")
            m = re.search(r"url=(.+)", content, re.I)
            if m:
                dest = m.group(1).strip("'\" ")
                if "downloadwella" in dest:
                    return dest

        # Raw scan of body
        m = re.search(r'https?://(?:www\.)?downloadwella\.com/[^\s\'"<>\\]+', body)
        if m:
            return m.group(0).rstrip(".,;)")

        # Redirect chain
        for resp in list(r.history) + [r]:
            if "downloadwella.com" in resp.url:
                return resp.url

        # Skip/continue button
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True).lower()
            if any(kw in text for kw in ["skip", "continue", "get link", "proceed"]):
                dest = a["href"]
                if dest.startswith("http"):
                    time.sleep(2)
                    r2, body2 = fetch(s, dest, referer=omg_url)
                    m = re.search(r'https?://(?:www\.)?downloadwella\.com/[^\s\'"<>\\]+', body2)
                    if m:
                        return m.group(0).rstrip(".,;)")

        log.warning(f"  omg10 could not resolve. Body snippet: {body[:200]}")
        return None
    except Exception as e:
        log.error(f"  omg10 error: {e}")
        return None


# ── Resolve downloadwella → direct file ──────────────────────────────────────
def resolve_downloadwella(s, dw_url):
    log.info(f"  dw: {dw_url}")
    host_base = urlparse(dw_url).scheme + "://" + urlparse(dw_url).netloc
    try:
        r1, body1 = fetch(s, dw_url, referer=BASE)
        soup1 = BeautifulSoup(body1, "lxml")

        form = soup1.find("form")
        if not form:
            return None

        data = {
            i.get("name"): i.get("value", "")
            for i in form.find_all("input") if i.get("name")
        }
        data["op"] = "download2"
        data["method_free"] = "Free Download"
        data.pop("method_premium", None)

        action = form.get("action", "").strip()
        post_url = urljoin(host_base, action) if action else dw_url

        r2, body2 = post_form(s, post_url, data, referer=dw_url)

        for resp in list(r2.history) + [r2]:
            if FILE_EXT_RE.search(resp.url):
                return resp.url

        soup2 = BeautifulSoup(body2, "lxml")
        for a in soup2.find_all("a", href=True):
            if FILE_EXT_RE.search(a["href"]) and a["href"].startswith("http"):
                return a["href"]

        for script in soup2.find_all("script"):
            t = script.string or ""
            m = re.search(r'["\']((https?://[^"\']{10,}\.(?:mp4|mkv|avi|webm))[^"\']*)["\']', t, re.I)
            if m:
                return m.group(1)

        m = re.search(r'https?://[^\s"\'<>]+\.(?:mp4|mkv|avi|webm)[^\s"\'<>]*', body2, re.I)
        if m:
            return m.group(0)

        return None
    except Exception as e:
        log.error(f"  dw error: {e}")
        return None


# ── Full chain ────────────────────────────────────────────────────────────────
def get_download_links(s, nkiri_url):
    r, body = fetch(s, nkiri_url)
    soup = BeautifulSoup(body, "lxml")
    info = parse_movie_info(soup, nkiri_url)
    title = info.get("title", "")

    omg_links = find_omg10_links(soup, body)
    log.info(f"Found {len(omg_links)} omg10 links")

    results = []
    for item in omg_links:
        dw_url = resolve_omg10(s, item["url"])
        if not dw_url:
            results.append({
                "label": item["label"], "url": item["url"],
                "host": "omg10.com", "note": "ad interstitial blocked on cloud IP",
            })
            continue

        direct = resolve_downloadwella(s, dw_url)
        if direct:
            filename = unquote(direct.split("/")[-1].split("?")[0])
            results.append({
                "label": item["label"] or filename,
                "url": direct,
                "host": urlparse(direct).netloc.replace("www.", ""),
                "filename": filename, "size": "",
            })
        else:
            results.append({
                "label": item["label"], "url": dw_url,
                "host": "downloadwella.com",
                "note": "downloadwella POST did not return direct link",
            })

    # Plain direct links in content
    content = soup.select_one(".entry-content, .post-content") or soup
    seen_urls = {r["url"] for r in results}
    for a in content.find_all("a", href=True):
        href = a["href"].strip()
        if FILE_EXT_RE.search(href) and href.startswith("http") and href not in seen_urls:
            if "thenkiri" not in href:
                results.append({
                    "label": a.get_text(strip=True) or "Direct Download",
                    "url": href,
                    "host": urlparse(href).netloc.replace("www.", ""),
                    "filename": href.split("/")[-1].split("?")[0], "size": "",
                })

    return title, results


# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.route("/api/movies")
@limiter.limit("30 per minute")
def all_movies():
    page = request.args.get("page", "1").strip()
    cat = request.args.get("category", "").strip()
    if cat:
        url = f"{BASE}/category/{cat}/" if page == "1" else f"{BASE}/category/{cat}/page/{page}/"
    else:
        url = BASE if page == "1" else f"{BASE}/page/{page}/"

    log.info(f"MOVIES cat={cat!r} page={page}")
    try:
        s = make_session()
        _, body = fetch(s, url)
    except Exception as e:
        return jsonify({"error": "Failed to reach Nkiri", "detail": str(e)}), 502

    soup = BeautifulSoup(body, "lxml")
    results = parse_movie_cards(soup)
    has_next = bool(soup.select_one("a.next, a[rel='next'], .nav-next a, .next.page-numbers"))

    cats, seen_slugs = [], set()
    for a in soup.find_all("a", href=True):
        m = re.search(r"/category/([^/]+)/?$", a.get("href", ""))
        if m and m.group(1) not in seen_slugs and "uncategorized" not in m.group(1):
            seen_slugs.add(m.group(1))
            cats.append({"name": a.get_text(strip=True), "slug": m.group(1)})

    return jsonify({
        "results": results, "count": len(results), "page": page,
        "next_page": str(int(page) + 1) if has_next else None,
        "categories": cats,
    })


@app.route("/api/search")
@limiter.limit("30 per minute")
def search():
    q = request.args.get("q", "").strip()
    page = request.args.get("page", "1").strip()
    if not q or len(q) < 2:
        return jsonify({"error": "Query too short"}), 400
    url = f"{BASE}/?s={q.replace(' ', '+')}&paged={page}"
    log.info(f"SEARCH q={q!r}")
    try:
        s = make_session()
        _, body = fetch(s, url)
    except Exception as e:
        return jsonify({"error": "Failed to reach Nkiri", "detail": str(e)}), 502
    soup = BeautifulSoup(body, "lxml")
    return jsonify({"results": parse_movie_cards(soup), "query": q, "page": page})


@app.route("/api/movie")
@limiter.limit("30 per minute")
def movie_info():
    page_url = request.args.get("url", "").strip()
    if not page_url:
        return jsonify({"error": "url required"}), 400
    if not is_nkiri(page_url):
        return jsonify({"error": "Only thenkiri.com.ng URLs"}), 400
    log.info(f"MOVIE {page_url}")
    try:
        s = make_session()
        _, body = fetch(s, page_url)
    except Exception as e:
        return jsonify({"error": "Failed to fetch", "detail": str(e)}), 502
    return jsonify(parse_movie_info(BeautifulSoup(body, "lxml"), page_url))


@app.route("/api/download")
@limiter.limit("15 per minute")
def download():
    page_url = request.args.get("url", "").strip()
    if not page_url:
        return jsonify({"error": "url required"}), 400
    if not is_nkiri(page_url):
        return jsonify({"error": "Only thenkiri.com.ng URLs"}), 400
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
        r, body = fetch(s, url)
        soup = BeautifulSoup(body, "lxml")

        omg_links = find_omg10_links(soup, body)
        dw_in_html = re.findall(r'https?://(?:www\.)?downloadwella\.com/[^\s\'"<>\\]+', body)

        # Full entry-content HTML — key to seeing how download buttons are structured
        content_el = soup.select_one(".entry-content, .post-content")
        content_html = str(content_el)[:8000] if content_el else ""

        # Data attributes on all tags
        data_attrs = []
        for tag in soup.find_all(True):
            for attr in ["data-link", "data-url", "data-href", "data-src",
                         "data-file", "data-download", "data-id"]:
                val = tag.get(attr, "")
                if val and len(val) > 5:
                    data_attrs.append({"tag": tag.name, "attr": attr, "value": val[:200]})

        # onclick handlers
        onclick_urls = []
        for tag in soup.find_all(True, onclick=True):
            oc = tag.get("onclick", "")
            if "http" in oc:
                onclick_urls.append({"tag": tag.name, "onclick": oc[:200]})

        # atob / base64 in scripts
        atob_decoded = []
        pat_atob = re.compile(r'atob\(["\']([ A-Za-z0-9+/=]{20,})["\']\)')
        for script in soup.find_all("script"):
            t = script.string or ""
            for m in pat_atob.finditer(t):
                try:
                    atob_decoded.append(base64.b64decode(m.group(1)).decode(errors="replace"))
                except Exception:
                    pass

        return jsonify({
            "status_code": r.status_code,
            "content_encoding": r.headers.get("Content-Encoding", "none"),
            "decoded_html_length": len(body),
            "entry_content_html": content_html,
            "omg10_links": omg_links,
            "downloadwella_in_html": dw_in_html,
            "data_attributes": data_attrs[:30],
            "onclick_with_urls": onclick_urls[:10],
            "atob_decoded": atob_decoded[:5],
            "all_links": [{"text": a.get_text(strip=True)[:80], "href": a["href"][:200]}
                          for a in soup.find_all("a", href=True)][:80],
            "all_forms": [
                {"action": f.get("action"), "method": f.get("method"),
                 "inputs": [{"type": i.get("type"), "name": i.get("name"),
                             "value": (i.get("value") or "")[:100]}
                            for i in f.find_all("input")]}
                for f in soup.find_all("form")
            ],
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
