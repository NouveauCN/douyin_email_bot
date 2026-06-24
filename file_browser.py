"""LAN web file browser for Douyin downloads.

Serves the downloads directory over HTTP with a dark-themed web UI:
- Browse videos by author folder
- HTML5 video player with seek support
- Slideshow image gallery with keyboard navigation
- Direct file download links

Usage:
    python file_browser.py [--port 8081] [--host 0.0.0.0]
"""

import argparse
import logging
import os
import re
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from flask import Flask, abort, render_template_string, request, send_from_directory

# ── Bootstrap ────────────────────────────────────────────────────────
_PROJECT_DIR = Path(__file__).parent

_env_path = _PROJECT_DIR / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

from config_loader import load_config  # noqa: E402

_config = load_config(_PROJECT_DIR / "config.yaml")
_DOWNLOAD_DIR = Path(_config.douyin.download_path)

# ── App setup ─────────────────────────────────────────────────────────

app = Flask(__name__)
log = logging.getLogger("file_browser")

# Regex: {YYYYMMDD}_{aweme_id}_{NN}.{ext} → capture prefix
_SLIDE_RE = re.compile(r"^(\d{8}_\d+)_\d+\.\w+$")


# ── Helpers ───────────────────────────────────────────────────────────

def _safe_subpath(subpath: str) -> Path:
    """Resolve subpath relative to download dir; reject traversal attempts."""
    p = (_DOWNLOAD_DIR / subpath).resolve()
    if _DOWNLOAD_DIR not in p.parents and p != _DOWNLOAD_DIR.resolve():
        abort(403, "Path traversal denied")
    return p


def _format_size(size_bytes: int) -> str:
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}" if unit != "B" else f"{size_bytes} B"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def _format_date(prefix: str) -> str:
    """YYYYMMDD → YYYY-MM-DD."""
    try:
        return f"{prefix[:4]}-{prefix[4:6]}-{prefix[6:8]}"
    except (IndexError, ValueError):
        return prefix


def _scan_downloads() -> dict:
    """Scan the downloads directory and return structured data."""
    authors = []
    slides = []
    slide_groups: dict[str, list[dict]] = {}

    if not _DOWNLOAD_DIR.is_dir():
        return {"authors": authors, "slides": slides, "empty": True}

    for entry in sorted(_DOWNLOAD_DIR.iterdir()):
        if entry.is_dir():
            if entry.name == "slides":
                # Group slideshow files by prefix
                for img in sorted(entry.iterdir()):
                    if not img.is_file():
                        continue
                    m = _SLIDE_RE.match(img.name)
                    if m:
                        prefix = m.group(1)
                        slide_groups.setdefault(prefix, []).append({
                            "name": img.name,
                            "size": img.stat().st_size,
                        })
                # Build slides list
                for prefix in sorted(slide_groups.keys(), reverse=True):
                    images = slide_groups[prefix]
                    total_size = sum(i["size"] for i in images)
                    slides.append({
                        "prefix": prefix,
                        "date": _format_date(prefix),
                        "image_count": len(images),
                        "total_size": total_size,
                        "size_fmt": _format_size(total_size),
                        "first_image": f"slides/{images[0]['name']}",
                    })
            else:
                # Author folder
                videos = []
                for vid in sorted(entry.iterdir()):
                    if vid.is_file():
                        videos.append({
                            "name": vid.name,
                            "size": vid.stat().st_size,
                            "size_fmt": _format_size(vid.stat().st_size),
                            "date": _format_date(vid.name[:8]) if len(vid.name) >= 8 else "",
                        })
                if videos:
                    total_size = sum(v["size"] for v in videos)
                    authors.append({
                        "name": entry.name,
                        "path": entry.name,
                        "video_count": len(videos),
                        "total_size": total_size,
                        "size_fmt": _format_size(total_size),
                    })

    return {
        "authors": authors,
        "slides": slides,
        "empty": not authors and not slides,
    }


def _mime_type(filepath: str) -> str:
    """Map file extension to MIME type."""
    ext = Path(filepath).suffix.lower()
    return {
        ".mp4": "video/mp4",
        ".webp": "image/webp",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
    }.get(ext, "application/octet-stream")


def _collect_videos(author: str | None = None) -> list[dict]:
    """Collect all .mp4 videos, optionally filtered by author folder."""
    videos = []
    if not _DOWNLOAD_DIR.is_dir():
        return videos
    for entry in sorted(_DOWNLOAD_DIR.iterdir()):
        if not entry.is_dir() or entry.name == "slides":
            continue
        if author and entry.name != author:
            continue
        for vid in sorted(entry.iterdir()):
            if vid.is_file() and vid.suffix.lower() == ".mp4":
                relpath = str(vid.relative_to(_DOWNLOAD_DIR)).replace("\\", "/")
                videos.append({
                    "name": vid.name,
                    "author": entry.name,
                    "relpath": relpath,
                    "size": vid.stat().st_size,
                    "size_fmt": _format_size(vid.stat().st_size),
                    "date": _format_date(vid.name[:8]) if len(vid.name) >= 8 else "",
                })
    return videos


# ── Routes ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Top-level index: author folders + slideshow groups."""
    data = _scan_downloads()
    return render_template_string(INDEX_HTML, **data)


@app.route("/browse/<path:subpath>")
def browse(subpath):
    """List files inside an author or slides folder."""
    target = _safe_subpath(subpath)

    if not target.is_dir():
        abort(404, "Directory not found")

    entries = []
    for f in sorted(target.iterdir()):
        if f.is_file():
            entries.append({
                "name": f.name,
                "size": f.stat().st_size,
                "size_fmt": _format_size(f.stat().st_size),
                "relpath": str(f.relative_to(_DOWNLOAD_DIR)).replace("\\", "/"),
                "is_video": f.suffix.lower() == ".mp4",
                "is_image": f.suffix.lower() in (".webp", ".jpg", ".jpeg", ".png", ".gif"),
                "date": _format_date(f.name[:8]) if len(f.name) >= 8 else "",
            })

    # Determine parent context
    parent_name = target.name
    parent_link = "/"

    return render_template_string(
        BROWSE_HTML,
        parent_name=parent_name,
        parent_link=parent_link,
        entries=entries,
        subpath=subpath,
        empty=len(entries) == 0,
    )


@app.route("/video/<path:filepath>")
def view_video(filepath):
    """Dedicated HTML5 video player page."""
    safe = _safe_subpath(filepath)
    if not safe.is_file():
        abort(404, "Video not found")

    relpath = str(safe.relative_to(_DOWNLOAD_DIR)).replace("\\", "/")
    filename = safe.name
    size_fmt = _format_size(safe.stat().st_size)
    date = _format_date(filename[:8]) if len(filename) >= 8 else ""
    parent = safe.parent.name

    return render_template_string(
        VIDEO_HTML,
        filename=filename,
        relpath=relpath,
        size_fmt=size_fmt,
        date=date,
        parent=parent,
        parent_path=quote(parent, safe=""),
        mime=_mime_type(filename),
    )


@app.route("/slideshow/<prefix>")
def view_slideshow(prefix):
    """Image gallery for a slideshow group."""
    slides_dir = _DOWNLOAD_DIR / "slides"
    if not slides_dir.is_dir():
        abort(404, "Slides directory not found")

    # Find all images with this prefix
    images = []
    for img in sorted(slides_dir.iterdir()):
        if img.is_file() and img.name.startswith(prefix + "_"):
            images.append({
                "name": img.name,
                "relpath": f"slides/{img.name}",
                "size_fmt": _format_size(img.stat().st_size),
                "index": len(images),
            })

    if not images:
        abort(404, "Slideshow not found")

    return render_template_string(
        SLIDESHOW_HTML,
        prefix=prefix,
        date=_format_date(prefix),
        images=images,
        total=len(images),
    )


@app.route("/playlist")
def playlist():
    """Auto-play playlist with shuffle, prev/next, and keyboard shortcuts."""
    author = request.args.get("author", "")
    videos = _collect_videos(author if author else None)

    if not videos:
        return render_template_string(
            PLAYLIST_EMPTY_HTML,
            author=author,
        )

    import json
    videos_json = json.dumps(videos, ensure_ascii=False)

    return render_template_string(
        PLAYLIST_HTML,
        videos=videos,
        videos_json=videos_json,
        total=len(videos),
        author=author,
        title=f"🎬 {author} · 全部播放" if author else "🎬 全部播放",
    )


@app.route("/raw/<path:filepath>")
def raw_file(filepath):
    """Serve raw file bytes with correct Content-Type and Range support."""
    safe = _safe_subpath(filepath)
    if not safe.is_file():
        abort(404, "File not found")

    directory = str(safe.parent)
    filename = safe.name
    return send_from_directory(directory, filename, mimetype=_mime_type(filename))


# ── Error handlers ────────────────────────────────────────────────────

@app.errorhandler(403)
def _forbidden(e):
    return render_template_string(ERROR_HTML, code=403, message=str(e)), 403


@app.errorhandler(404)
def _not_found(e):
    return render_template_string(ERROR_HTML, code=404, message="Page not found"), 404


# ── Templates ─────────────────────────────────────────────────────────

# Shared CSS
_COMMON_CSS = """
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
    background: #f5f5f5; color: #333;
    min-height: 100vh; padding: 20px;
  }
  .container { max-width: 960px; margin: 0 auto; }
  h1 { font-size: 22px; font-weight: 600; color: #111; margin-bottom: 4px; }
  .subtitle { font-size: 13px; color: #999; margin-bottom: 24px; }
  .card-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
    gap: 14px; margin-bottom: 32px;
  }
  .card {
    background: #fff; border-radius: 12px; padding: 20px;
    text-decoration: none; color: #333; display: block;
    transition: background 0.15s, transform 0.15s;
    box-shadow: 0 1px 4px rgba(0,0,0,0.06);
  }
  .card:hover { background: #fafafa; transform: translateY(-1px); }
  .card h3 { font-size: 16px; color: #111; margin-bottom: 6px; }
  .card .meta { font-size: 12px; color: #999; line-height: 1.6; }
  .card .icon { font-size: 28px; margin-bottom: 10px; }
  .section-title {
    font-size: 16px; font-weight: 600; color: #888; margin-bottom: 14px;
    padding-bottom: 8px; border-bottom: 1px solid #e8e8e8;
  }
  .empty-state { text-align: center; padding: 60px 20px; color: #999; }
  .empty-state .icon { font-size: 48px; margin-bottom: 16px; }
  .back-link {
    display: inline-block; color: #999; text-decoration: none; font-size: 13px;
    margin-bottom: 16px; transition: color 0.15s;
  }
  .back-link:hover { color: #fe2c55; }
  .btn {
    display: inline-block; padding: 10px 24px; border: none; border-radius: 8px;
    font-size: 14px; cursor: pointer; background: #fe2c55; color: #fff;
    text-decoration: none; transition: opacity 0.2s;
  }
  .btn:hover { opacity: 0.85; }
  video:focus, img:focus { outline: none; }
"""

INDEX_HTML = (
    "<!DOCTYPE html>\n"
    '<html lang="zh-CN">\n<head>\n'
    '<meta charset="UTF-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
    "<title>下载浏览 — Douyin Email Bot</title>\n"
    "<style>" + _COMMON_CSS + """
  .stat { display: inline-block; font-size: 12px; color: #888;
          background: #eee; padding: 2px 8px; border-radius: 4px; margin-right: 4px; }
</style>
</head>
<body>
<div class="container">
  <h1>📦 下载浏览</h1>
  <p class="subtitle">Douyin Email Bot — LAN File Browser</p>

  {% if authors %}
  <div style="margin-bottom:24px">
    <a class="btn" href="{{ url_for('playlist') }}">▶ 全部播放（随机）</a>
  </div>
  {% endif %}

  {% if empty %}
  <div class="empty-state">
    <div class="icon">📭</div>
    <p>暂无下载内容</p>
    <p style="font-size:13px;margin-top:8px">发送抖音链接到邮箱，机器人会自动下载</p>
  </div>
  {% endif %}

  {% if authors %}
  <div class="section-title">📹 视频 · {{ authors | length }} 位作者</div>
  <div class="card-grid">
  {% for a in authors %}
    <a class="card" href="{{ url_for('browse', subpath=a.path) }}">
      <div class="icon">🎬</div>
      <h3>{{ a.name }}</h3>
      <div class="meta">
        <span class="stat">{{ a.video_count }} 个视频</span>
        <span class="stat">{{ a.size_fmt }}</span>
      </div>
    </a>
  {% endfor %}
  </div>
  {% endif %}

  {% if slides %}
  <div class="section-title">🖼️ 图集 · {{ slides | length }} 组</div>
  <div class="card-grid">
  {% for s in slides %}
    <a class="card" href="{{ url_for('view_slideshow', prefix=s.prefix) }}">
      <div class="icon">🖼️</div>
      <h3>{{ s.date }}</h3>
      <div class="meta">
        <span class="stat">{{ s.image_count }} 张图片</span>
        <span class="stat">{{ s.size_fmt }}</span>
      </div>
    </a>
  {% endfor %}
  </div>
  {% endif %}
</div>
</body>
</html>"""  # noqa: E501
)

BROWSE_HTML = (
    "<!DOCTYPE html>\n"
    '<html lang="zh-CN">\n<head>\n'
    '<meta charset="UTF-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
    "<title>{{ parent_name }} — 下载浏览</title>\n"
    "<style>" + _COMMON_CSS + """
  .file-row {
    display: flex; align-items: center; gap: 14px;
    background: #fff; border-radius: 10px; padding: 14px 18px;
    margin-bottom: 8px; transition: background 0.15s;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
  }
  .file-row:hover { background: #fafafa; }
  .file-icon { font-size: 24px; flex-shrink: 0; width: 36px; text-align: center; }
  .file-info { flex: 1; min-width: 0; }
  .file-info .fname { font-size: 14px; color: #333; word-break: break-all; }
  .file-info .fmeta { font-size: 12px; color: #999; margin-top: 2px; }
  .file-action { flex-shrink: 0; }
  .play-btn {
    display: inline-block; padding: 6px 16px; border-radius: 6px;
    background: #fe2c55; color: #fff; text-decoration: none; font-size: 13px;
    transition: opacity 0.15s;
  }
  .play-btn:hover { opacity: 0.85; }
  .dl-btn {
    display: inline-block; padding: 6px 12px; border-radius: 6px;
    background: #eee; color: #666; text-decoration: none; font-size: 12px;
    margin-left: 6px; transition: background 0.15s;
  }
  .dl-btn:hover { background: #ddd; }
</style>
</head>
<body>
<div class="container">
  <a class="back-link" href="{{ url_for('index') }}">← 返回首页</a>
  <h1>📁 {{ parent_name }}</h1>
  <p class="subtitle">{{ entries | length }} 个文件</p>

  {% if entries | selectattr('is_video') | list | length > 0 %}
  <div style="margin-bottom:18px">
    <a class="btn" href="{{ url_for('playlist', author=parent_name) }}">▶ 播放全部（随机）</a>
  </div>
  {% endif %}

  {% if empty %}
  <div class="empty-state">
    <div class="icon">📂</div>
    <p>此目录为空</p>
  </div>
  {% endif %}

  {% for f in entries %}
  <div class="file-row">
    <div class="file-icon">{{ '🎬' if f.is_video else '🖼️' if f.is_image else '📄' }}</div>
    <div class="file-info">
      <div class="fname">{{ f.name }}</div>
      <div class="fmeta">{{ f.date }} · {{ f.size_fmt }}</div>
    </div>
    <div class="file-action">
      {% if f.is_video %}
      <a class="play-btn" href="{{ url_for('view_video', filepath=f.relpath) }}">▶ 播放</a>
      {% endif %}
      <a class="dl-btn" href="{{ url_for('raw_file', filepath=f.relpath) }}" download>⬇ 下载</a>
    </div>
  </div>
  {% endfor %}
</div>
</body>
</html>"""  # noqa: E501
)

VIDEO_HTML = (
    "<!DOCTYPE html>\n"
    '<html lang="zh-CN">\n<head>\n'
    '<meta charset="UTF-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
    "<title>{{ filename }} — 视频播放</title>\n"
    "<style>" + _COMMON_CSS + """
  .video-wrapper {
    background: #000; border-radius: 12px; overflow: hidden;
    margin-bottom: 16px; box-shadow: 0 4px 20px rgba(0,0,0,0.5);
  }
  .video-wrapper video { width: 100%; display: block; max-height: 70vh; }
  .info-bar {
    display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
    background: #fff; border-radius: 10px; padding: 16px 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
  }
  .info-item { font-size: 13px; color: #999; }
  .info-item strong { color: #333; }
  .actions { margin-top: 14px; display: flex; gap: 10px; flex-wrap: wrap; }
</style>
</head>
<body>
<div class="container">
  <a class="back-link" href="{{ url_for('browse', subpath=parent_path) }}">← 返回 {{ parent }}</a>
  <h1>🎬 {{ filename }}</h1>
  <p class="subtitle">{{ date }}</p>

  <div class="video-wrapper">
    <video controls autoplay playsinline>
      <source src="{{ url_for('raw_file', filepath=relpath) }}" type="{{ mime }}">
      您的浏览器不支持 HTML5 视频播放
    </video>
  </div>

  <div class="info-bar">
    <div class="info-item">📦 <strong>{{ size_fmt }}</strong></div>
    <div class="info-item">📅 <strong>{{ date }}</strong></div>
    <div class="info-item">📁 <strong>{{ parent }}</strong></div>
  </div>

  <div class="actions">
    <a class="btn" href="{{ url_for('raw_file', filepath=relpath) }}" download>⬇ 下载视频</a>
    <a class="btn" href="{{ url_for('browse', subpath=parent_path) }}" style="background:#eee;color:#555">📂 查看更多</a>
  </div>
</div>
</body>
</html>"""  # noqa: E501
)

SLIDESHOW_HTML = (
    "<!DOCTYPE html>\n"
    '<html lang="zh-CN">\n<head>\n'
    '<meta charset="UTF-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
    "<title>{{ prefix }} — 图集浏览</title>\n"
    "<style>" + _COMMON_CSS + """
  .gallery-wrapper {
    background: #000; border-radius: 12px; overflow: hidden;
    margin-bottom: 16px; box-shadow: 0 4px 20px rgba(0,0,0,0.5);
    text-align: center; position: relative;
    min-height: 300px; display: flex; align-items: center; justify-content: center;
  }
  .gallery-wrapper img {
    max-width: 100%; max-height: 70vh; object-fit: contain;
    transition: opacity 0.2s;
  }
  .nav-btn {
    position: absolute; top: 50%; transform: translateY(-50%);
    background: rgba(0,0,0,0.6); color: #fff; border: none;
    font-size: 32px; width: 48px; height: 48px; border-radius: 50%;
    cursor: pointer; z-index: 2; line-height: 1;
    transition: background 0.15s;
  }
  .nav-btn:hover { background: rgba(254,44,85,0.7); }
  .nav-btn:disabled { opacity: 0.2; cursor: default; }
  .nav-prev { left: 12px; }
  .nav-next { right: 12px; }
  .gallery-info {
    display: flex; flex-wrap: wrap; gap: 10px; align-items: center; justify-content: center;
    background: #fff; border-radius: 10px; padding: 16px 20px; margin-bottom: 14px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
  }
  .gallery-info .counter { font-size: 18px; font-weight: 600; color: #fe2c55; }
  .gallery-info .meta { font-size: 13px; color: #999; }
  .thumb-strip {
    display: flex; gap: 6px; overflow-x: auto; padding: 8px 0;
    scroll-behavior: smooth;
  }
  .thumb-strip img {
    width: 60px; height: 60px; object-fit: cover; border-radius: 6px;
    cursor: pointer; border: 2px solid transparent; opacity: 0.5;
    transition: opacity 0.15s, border-color 0.15s; flex-shrink: 0;
  }
  .thumb-strip img.active { border-color: #fe2c55; opacity: 1; }
  .thumb-strip img:hover { opacity: 0.85; }
  .key-hint { font-size: 11px; color: #999; text-align: center; margin-top: 10px; }
</style>
</head>
<body>
<div class="container">
  <a class="back-link" href="{{ url_for('index') }}">← 返回首页</a>
  <h1>🖼️ 图集</h1>
  <p class="subtitle">{{ date }} · {{ total }} 张图片</p>

  <div class="gallery-wrapper">
    <button class="nav-btn nav-prev" id="prevBtn" onclick="navigate(-1)">‹</button>
    <img id="mainImg" src="{{ url_for('raw_file', filepath=images[0].relpath) }}" alt="">
    <button class="nav-btn nav-next" id="nextBtn" onclick="navigate(1)">›</button>
  </div>

  <div class="gallery-info">
    <span class="counter" id="counter">1 / {{ total }}</span>
    <span class="meta" id="imgName">{{ images[0].name }}</span>
    <span class="meta" id="imgSize">{{ images[0].size_fmt }}</span>
  </div>

  <div class="thumb-strip" id="thumbStrip">
    {% for img in images %}
    <img src="{{ url_for('raw_file', filepath=img.relpath) }}"
         class="{{ 'active' if loop.first }}"
         data-index="{{ img.index }}"
         onclick="jumpTo({{ img.index }})"
         title="{{ img.name }}">
    {% endfor %}
  </div>
  <p class="key-hint">💡 使用 ← → 方向键或点击缩略图切换图片</p>
</div>

<script>
const IMAGES = [
{% for img in images %}
  {name: "{{ img.name }}", relpath: "{{ img.relpath }}", sizeFmt: "{{ img.size_fmt }}"}{{ "," if not loop.last else "" }}
{% endfor %}
];
let _idx = 0;
const mainImg = document.getElementById('mainImg');
const counter = document.getElementById('counter');
const imgName = document.getElementById('imgName');
const imgSize = document.getElementById('imgSize');
const prevBtn = document.getElementById('prevBtn');
const nextBtn = document.getElementById('nextBtn');
const thumbs = document.querySelectorAll('#thumbStrip img');

function show(i) {
  _idx = Math.max(0, Math.min(i, IMAGES.length - 1));
  const img = IMAGES[_idx];
  mainImg.src = "{{ url_for('raw_file', filepath='') }}" + img.relpath;
  counter.textContent = (_idx + 1) + " / " + IMAGES.length;
  imgName.textContent = img.name;
  imgSize.textContent = img.sizeFmt;
  prevBtn.disabled = (_idx === 0);
  nextBtn.disabled = (_idx === IMAGES.length - 1);
  thumbs.forEach(t => t.classList.toggle('active', parseInt(t.dataset.index) === _idx));
  // Scroll thumbnail into view
  const activeThumb = document.querySelector('#thumbStrip img.active');
  if (activeThumb) activeThumb.scrollIntoView({behavior: 'smooth', block: 'nearest', inline: 'center'});
}

function navigate(delta) { show(_idx + delta); }
function jumpTo(i) { show(i); }

document.addEventListener('keydown', function(e) {
  if (e.key === 'ArrowLeft') { e.preventDefault(); navigate(-1); }
  if (e.key === 'ArrowRight') { e.preventDefault(); navigate(1); }
});
</script>
</body>
</html>"""  # noqa: E501
)

PLAYLIST_EMPTY_HTML = (
    "<!DOCTYPE html>\n"
    '<html lang="zh-CN">\n<head>\n'
    '<meta charset="UTF-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
    "<title>播放列表 — 无视频</title>\n"
    "<style>" + _COMMON_CSS + """
</style>
</head>
<body>
<div class="container">
  <a class="back-link" href="{{ url_for('index') }}">← 返回首页</a>
  <div class="empty-state">
    <div class="icon">📭</div>
    <p>{% if author %}<b>{{ author }}</b> 中没有视频{% else %}暂无下载视频{% endif %}</p>
    <p style="font-size:13px;margin-top:8px">发送抖音链接到邮箱，机器人会自动下载</p>
  </div>
</div>
</body>
</html>"""
)

PLAYLIST_HTML = (
    "<!DOCTYPE html>\n"
    '<html lang="zh-CN">\n<head>\n'
    '<meta charset="UTF-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
    "<title>{{ title }}</title>\n"
    "<style>" + _COMMON_CSS + """
  .player-section { margin-bottom: 16px; }
  .video-wrapper {
    background: #000; border-radius: 12px; overflow: hidden;
    box-shadow: 0 4px 20px rgba(0,0,0,0.5);
  }
  .video-wrapper video { width: 100%; display: block; max-height: 60vh; }
  .now-playing {
    background: #fff; border-radius: 10px; padding: 14px 18px; margin-top: 12px;
    display: flex; flex-wrap: wrap; gap: 12px; align-items: center; justify-content: space-between;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
  }
  .now-playing .info { font-size: 13px; color: #999; }
  .now-playing .info strong { color: #333; }
  .now-playing .info .author-tag {
    display: inline-block; background: #fe2c55; color: #fff; font-size: 11px;
    padding: 2px 8px; border-radius: 4px; margin-right: 8px;
  }
  .controls {
    display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
    margin: 14px 0;
  }
  .ctrl-btn {
    padding: 8px 18px; border-radius: 8px; border: none; font-size: 13px;
    cursor: pointer; background: #eee; color: #555;
    transition: background 0.15s; text-decoration: none; display: inline-block;
  }
  .ctrl-btn:hover { background: #ddd; }
  .ctrl-btn.primary { background: #fe2c55; color: #fff; }
  .ctrl-btn.primary:hover { opacity: 0.85; }
  .ctrl-btn:disabled { opacity: 0.3; cursor: not-allowed; }
  .playlist-panel { margin-top: 20px; }
  .playlist-header {
    font-size: 14px; font-weight: 600; color: #888; margin-bottom: 10px;
    padding-bottom: 8px; border-bottom: 1px solid #e8e8e8;
    display: flex; justify-content: space-between; align-items: center;
  }
  .playlist-item {
    display: flex; align-items: center; gap: 10px;
    padding: 10px 14px; border-radius: 8px; cursor: pointer;
    transition: background 0.1s; margin-bottom: 2px;
  }
  .playlist-item:hover { background: #f5f5f5; }
  .playlist-item.current { background: #fff0f3; }
  .playlist-item.current .idx { color: #fe2c55; font-weight: 700; }
  .playlist-item .idx { width: 32px; text-align: right; font-size: 12px; color: #ccc; flex-shrink: 0; }
  .playlist-item .info { flex: 1; min-width: 0; }
  .playlist-item .info .vname { font-size: 13px; color: #333; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .playlist-item .info .vmeta { font-size: 11px; color: #999; }
  .playlist-item .vsize { font-size: 11px; color: #999; flex-shrink: 0; }
  .key-hint {
    font-size: 11px; color: #999; text-align: center; margin-top: 20px;
    padding: 12px; background: #f0f0f0; border-radius: 8px;
  }
  .key-hint kbd {
    display: inline-block; background: #e0e0e0; color: #666; padding: 1px 7px;
    border-radius: 4px; font-family: monospace; font-size: 11px; margin: 0 2px;
  }
</style>
</head>
<body>
<div class="container">
  {% if author %}
  <a class="back-link" href="{{ url_for('browse', subpath=author) }}">← 返回 {{ author }}</a>
  {% else %}
  <a class="back-link" href="{{ url_for('index') }}">← 返回首页</a>
  {% endif %}
  <h1>{{ title }}</h1>
  <p class="subtitle" id="statusLine">{{ total }} 个视频 · 随机播放</p>

  <!-- Video player -->
  <div class="player-section">
    <div class="video-wrapper">
      <video id="player" controls autoplay playsinline></video>
    </div>

    <div class="now-playing">
      <div class="info">
        <span id="npTitle">—</span>
      </div>
      <div class="info">
        <span id="npSize">—</span> · <span id="npDate">—</span>
      </div>
    </div>

    <div class="controls">
      <button class="ctrl-btn" id="prevBtn" onclick="prevVideo()" title="上一个">◀ 上一个</button>
      <button class="ctrl-btn primary" id="shuffleBtn" onclick="toggleShuffle()">
        🔀 <span id="shuffleLabel">随机: 开</span>
      </button>
      <button class="ctrl-btn" id="nextBtn" onclick="nextVideo()" title="下一个">下一个 ▶</button>
    </div>
  </div>

  <!-- Playlist -->
  <div class="playlist-panel">
    <div class="playlist-header">
      <span>📋 播放列表</span>
      <span style="font-size:12px;color:#666">{{ total }} 个视频</span>
    </div>
    <div id="playlist">
      {% for v in videos %}
      <div class="playlist-item" id="item-{{ loop.index0 }}" onclick="playIndex({{ loop.index0 }})">
        <span class="idx">{{ loop.index }}</span>
        <div class="info">
          <div class="vname">{{ v.name }}</div>
          <div class="vmeta"><span class="author-tag">{{ v.author }}</span>{{ v.date }}</div>
        </div>
        <span class="vsize">{{ v.size_fmt }}</span>
      </div>
      {% endfor %}
    </div>
  </div>

  <div class="key-hint">
    💡 键盘快捷键：
    <kbd>←</kbd> 上一个 &nbsp;
    <kbd>→</kbd> 下一个 &nbsp;
    <kbd>Space</kbd> 播放/暂停 &nbsp;
    <kbd>S</kbd> 切换随机
  </div>
</div>

<script>
// ── State ──
const VIDEOS = {{ videos_json | safe }};
let queue = VIDEOS.map((v, i) => i);
let currentQueueIdx = 0;
let shuffleOn = true;

// ── Shuffle (Fisher-Yates) ──
function shuffleArray(arr) {
  for (let i = arr.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [arr[i], arr[j]] = [arr[j], arr[i]];
  }
}

function buildQueue() {
  const currentVidIdx = currentVideoIndex();
  queue = VIDEOS.map((v, i) => i);
  if (shuffleOn) {
    const cur = queue.splice(currentVidIdx, 1)[0];
    shuffleArray(queue);
    queue.unshift(cur);
  }
  currentQueueIdx = 0;
}

function currentVideoIndex() {
  return queue[currentQueueIdx];
}

function currentVideo() {
  return VIDEOS[currentVideoIndex()];
}

// ── Playback ──
const player = document.getElementById('player');

function playIndex(queueIdx) {
  currentQueueIdx = queueIdx;
  const v = currentVideo();
  player.src = "{{ url_for('raw_file', filepath='') }}" + v.relpath;
  player.play().catch(function() {});
  updateUI();
}

function nextVideo() {
  if (currentQueueIdx < queue.length - 1) {
    playIndex(currentQueueIdx + 1);
  }
}

function prevVideo() {
  if (player.currentTime > 3) {
    player.currentTime = 0;
    player.play().catch(function() {});
    return;
  }
  if (currentQueueIdx > 0) {
    playIndex(currentQueueIdx - 1);
  }
}

function toggleShuffle() {
  shuffleOn = !shuffleOn;
  buildQueue();
  updateUI();
}

// ── UI ──
function updateUI() {
  const v = currentVideo();
  const vidIdx = currentVideoIndex();

  document.getElementById('npTitle').innerHTML =
    '<span class="author-tag">' + v.author + '</span><strong>' + v.name + '</strong>';
  document.getElementById('npSize').textContent = '📦 ' + v.size_fmt;
  document.getElementById('npDate').textContent = '📅 ' + v.date;

  document.getElementById('statusLine').textContent =
    (currentQueueIdx + 1) + ' / ' + queue.length +
    ' · ' + (shuffleOn ? '随机播放' : '顺序播放');

  document.getElementById('prevBtn').disabled = (currentQueueIdx === 0);
  document.getElementById('nextBtn').disabled = (currentQueueIdx >= queue.length - 1);

  var lbl = document.getElementById('shuffleLabel');
  var btn = document.getElementById('shuffleBtn');
  if (shuffleOn) {
    lbl.textContent = '随机: 开';
    btn.classList.add('primary');
  } else {
    lbl.textContent = '随机: 关';
    btn.classList.remove('primary');
  }

  document.querySelectorAll('.playlist-item').forEach(function(el) { el.classList.remove('current'); });
  var activeItem = document.getElementById('item-' + vidIdx);
  if (activeItem) {
    activeItem.classList.add('current');
    activeItem.scrollIntoView({behavior: 'smooth', block: 'nearest'});
  }
}

// ── Events ──
player.addEventListener('ended', function() {
  if (currentQueueIdx < queue.length - 1) {
    playIndex(currentQueueIdx + 1);
  }
});

player.addEventListener('error', function() {
  setTimeout(function() {
    if (currentQueueIdx < queue.length - 1) {
      nextVideo();
    }
  }, 1500);
});

document.addEventListener('keydown', function(e) {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

  switch (e.key) {
    case 'ArrowLeft':  e.preventDefault(); prevVideo(); break;
    case 'ArrowRight': e.preventDefault(); nextVideo(); break;
    case ' ':
      e.preventDefault();
      if (player.paused) player.play().catch(function() {});
      else player.pause();
      break;
    case 's': case 'S': e.preventDefault(); toggleShuffle(); break;
  }
});

// ── Init ──
if (shuffleOn) shuffleArray(queue);
if (queue.length > 0) {
  var firstV = VIDEOS[queue[0]];
  player.src = "{{ url_for('raw_file', filepath='') }}" + firstV.relpath;
  updateUI();
}
</script>
</body>
</html>"""  # noqa: E501
)

ERROR_HTML = (
    "<!DOCTYPE html>\n"
    '<html lang="zh-CN">\n<head>\n'
    '<meta charset="UTF-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
    "<title>{{ code }} — Error</title>\n"
    "<style>" + _COMMON_CSS + """
  .error-box { text-align: center; padding: 80px 20px; }
  .error-box .code { font-size: 72px; font-weight: 700; color: #fe2c55; }
  .error-box .msg { font-size: 16px; color: #888; margin: 12px 0 24px; }
</style>
</head>
<body>
<div class="container">
  <div class="error-box">
    <div class="code">{{ code }}</div>
    <div class="msg">{{ message }}</div>
    <a class="btn" href="{{ url_for('index') }}">← 返回首页</a>
  </div>
</div>
</body>
</html>"""
)


# ── Entrypoint ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Douyin File Browser")
    parser.add_argument("--port", type=int, default=8081, help="Listen port (default: 8081)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    log.info("Starting file browser on http://%s:%d", args.host, args.port)
    log.info("Serving downloads from: %s", _DOWNLOAD_DIR)

    try:
        app.run(host=args.host, port=args.port, debug=args.debug)
    except KeyboardInterrupt:
        log.info("Shutting down")


if __name__ == "__main__":
    main()
