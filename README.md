# Nkiri API

Three endpoints. Drop into any website.

---

## Endpoints

### 1. Search
```
GET /api/search?q=movie+name
GET /api/search?q=movie+name&page=2
```
Call when user types in your search box.

**Response:**
```json
{
  "results": [
    {
      "title": "Avengers Endgame",
      "url": "https://thenkiri.com.ng/avengers-endgame/",
      "thumbnail": "https://..."
    }
  ],
  "count": 10,
  "query": "avengers"
}
```

---

### 2. Movie Info
```
GET /api/movie?url=https://thenkiri.com.ng/movie-name/
```
Call when user clicks on a movie card. Returns title, poster, description, details.

**Response:**
```json
{
  "title": "Avengers Endgame",
  "url": "https://thenkiri.com.ng/avengers-endgame/",
  "thumbnail": "https://...",
  "description": "After the devastating events...",
  "details": {
    "year": "2019",
    "genre": "Action, Adventure",
    "size": "1.2GB"
  }
}
```

---

### 3. Download Links ← the important one
```
GET /api/download?url=https://thenkiri.com.ng/movie-name/
```
Call ONLY when user clicks the Download button. Always live — never cached — so links never expire.

**Response:**
```json
{
  "title": "Avengers Endgame",
  "url": "https://thenkiri.com.ng/avengers-endgame/",
  "links": [
    { "label": "Download 720p", "url": "https://mediafire.com/...", "host": "mediafire.com" },
    { "label": "Download 1080p", "url": "https://gofile.io/...", "host": "gofile.io" }
  ],
  "count": 2,
  "fetched_at": "2025-05-22T10:00:00Z"
}
```

---

## Deploy to Render

1. Push this folder to GitHub
2. Render → New → Blueprint → connect repo
3. Done. Live at `https://your-app.onrender.com`

Or manually:
- Build: `pip install -r requirements.txt`
- Start: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 60`

---

## Integrate into your website

```js
const API = 'https://your-app.onrender.com';

// 1. Search
async function searchMovies(query) {
  const res = await fetch(`${API}/api/search?q=${encodeURIComponent(query)}`);
  const data = await res.json();
  return data.results; // [{ title, url, thumbnail }]
}

// 2. Movie info (on card click)
async function getMovieInfo(moviePageUrl) {
  const res = await fetch(`${API}/api/movie?url=${encodeURIComponent(moviePageUrl)}`);
  return await res.json(); // { title, thumbnail, description, details }
}

// 3. Download links (on download button click) — always fresh
async function getDownloadLinks(moviePageUrl) {
  const res = await fetch(`${API}/api/download?url=${encodeURIComponent(moviePageUrl)}`);
  const data = await res.json();
  return data.links; // [{ label, url, host }]
}
```

---

## Rate Limits
- Search: 30/min
- Movie info: 30/min  
- Download: 20/min
