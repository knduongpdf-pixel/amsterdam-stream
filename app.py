"""
Amsterdam earthTV live stream — cloud web player.
Proxies the HLS stream through our server to bypass CDN CORS restrictions.
"""

import re
import time
import threading
from urllib.parse import urljoin, urlparse, quote, unquote

import requests
from flask import Flask, jsonify, render_template_string, Response, request

app = Flask(__name__)

EARTHTV_PAGE = "https://www.earthtv.com/en/webcam/amsterdam-city-skyline"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.earthtv.com/",
    "Origin": "https://www.earthtv.com",
}

_cache = {"url": None, "ts": 0}
_lock = threading.Lock()
CACHE_TTL = 70


def fetch_stream_url() -> str | None:
    try:
        resp = requests.get(EARTHTV_PAGE, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        match = re.search(r'<etv-player[^>]+token="([^"]+)"', resp.text)
        if not match:
            return None
        token = match.group(1)

        api_url = (
            "https://livecloud.earthtv.com/api/v1/media.getPlayerConfig"
            f"?playerToken={token}"
        )
        api = requests.get(api_url, headers=HEADERS, timeout=20)
        api.raise_for_status()
        data = api.json()

        # Extract from streamUris or fallback fields
        url = None
        if "streamUris" in data:
            uris = data["streamUris"]
            if isinstance(uris, list) and uris:
                url = uris[0]
            elif isinstance(uris, dict):
                url = next(iter(uris.values()), None)

        if not url:
            raw = str(data)
            m = re.search(r'https?://[^\s\'"]+\.m3u8[^\s\'"]*', raw)
            if m:
                url = m.group(0)

        if url:
            print(f"Stream URL: {url[:80]}...")
        return url

    except Exception as e:
        print(f"ERROR: {e}")
        return None


def get_cached_url() -> str | None:
    with _lock:
        now = time.time()
        if _cache["url"] and now - _cache["ts"] < CACHE_TTL:
            return _cache["url"]
        url = fetch_stream_url()
        if url:
            _cache["url"] = url
            _cache["ts"] = now
        return _cache["url"]


# ── HLS Proxy ─────────────────────────────────────────────────────────────────

def proxy_url(url: str) -> str:
    """Rewrite a CDN URL to go through our /proxy endpoint."""
    return "/proxy?u=" + quote(url, safe="")


def rewrite_m3u8(content: str, base_url: str) -> str:
    """Rewrite all URLs in an m3u8 playlist to go through our proxy."""
    lines = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            lines.append(line)
        elif stripped:
            # Resolve relative URLs against base
            absolute = urljoin(base_url, stripped)
            lines.append(proxy_url(absolute))
        else:
            lines.append(line)
    return "\n".join(lines)


@app.route("/proxy")
def proxy():
    url = unquote(request.args.get("u", ""))
    if not url or "earthtv" not in url and "fastly" not in url:
        return "Forbidden", 403
    try:
        r = requests.get(url, headers=HEADERS, timeout=20, stream=True)
        content_type = r.headers.get("Content-Type", "application/octet-stream")

        if "mpegurl" in content_type or url.endswith(".m3u8"):
            body = rewrite_m3u8(r.text, url)
            return Response(body, content_type="application/vnd.apple.mpegurl",
                            headers={"Access-Control-Allow-Origin": "*"})
        else:
            # Stream binary (video segments)
            return Response(
                r.iter_content(chunk_size=8192),
                content_type=content_type,
                headers={"Access-Control-Allow-Origin": "*"},
            )
    except Exception as e:
        return str(e), 502


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/stream-url")
def stream_url():
    url = get_cached_url()
    if not url:
        return jsonify({"error": "could not fetch stream URL"}), 503
    # Return proxied URL so browser goes through our server
    return jsonify({"url": proxy_url(url)})


PLAYER_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Amsterdam Live</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { background: #000; display: flex; flex-direction: column;
           align-items: center; justify-content: center; min-height: 100vh; }
    video { width: 100%; max-width: 1280px; max-height: 90vh; }
    p { color: #555; font-family: sans-serif; font-size: 13px; margin-top: 8px; }
  </style>
</head>
<body>
  <video id="v" autoplay playsinline controls muted></video>
<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
  <script>
    const v = document.getElementById('v');
    let hls = null;
    let currentUrl = null;

    function load(url) {
      if (url === currentUrl) return;
      currentUrl = url;
      if (hls) { hls.destroy(); hls = null; }
      if (Hls.isSupported()) {
        hls = new Hls();
        hls.loadSource(url);
        hls.attachMedia(v);
        hls.on(Hls.Events.MANIFEST_PARSED, () => v.play());
      } else if (v.canPlayType('application/vnd.apple.mpegurl')) {
        v.src = url;
        v.play();
      }
    }

    async function refresh() {
      try {
        const r = await fetch('/stream-url');
        const { url } = await r.json();
        if (url) load(url);
      } catch(e) {}
    }

    refresh();
    setInterval(refresh, 65000);
  </script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(PLAYER_HTML)


if __name__ == "__main__":
    get_cached_url()
    app.run(host="0.0.0.0", port=8080)
