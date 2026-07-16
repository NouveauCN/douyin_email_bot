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
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

from PIL import Image

from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    make_response,
    redirect,
    render_template_string,
    request,
    send_from_directory,
    url_for,
)

# ── Bootstrap ────────────────────────────────────────────────────────
_PROJECT_DIR = Path(__file__).parent

_env_path = _PROJECT_DIR / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

from config_loader import load_config  # noqa: E402

_config = load_config(_PROJECT_DIR / "config.yaml")
_DOWNLOAD_DIR = Path(_config.douyin.download_path)
_THUMB_CACHE = Path("/app/.thumb_cache")

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


def _cleanup_empty_parents(start: Path) -> list[Path]:
    """Remove empty parent directories under the download root."""
    removed = []
    root = _DOWNLOAD_DIR.resolve()
    current = start.resolve()

    while current != root and root in current.parents:
        try:
            current.rmdir()
        except FileNotFoundError:
            current = current.parent
            continue
        except OSError:
            break
        removed.append(current)
        current = current.parent

    return removed


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
    """Scan the downloads directory and return flat lists of videos and slides."""
    videos = []
    slides = []

    if not _DOWNLOAD_DIR.is_dir():
        return {"videos": videos, "slides": slides, "empty": True}

    for entry in sorted(_DOWNLOAD_DIR.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if entry.name == "slides":
            for img in sorted(entry.iterdir(), reverse=True):
                if img.is_file():
                    relpath = str(img.relative_to(_DOWNLOAD_DIR)).replace("\\", "/")
                    date_str = _format_date(img.name[:8]) if len(img.name) >= 8 else ""
                    slides.append({
                        "name": img.name,
                        "relpath": relpath,
                        "size": img.stat().st_size,
                        "size_fmt": _format_size(img.stat().st_size),
                        "date": date_str,
                    })
        else:
            # Author folder — collect all video files
            for vid in sorted(entry.iterdir(), reverse=True):
                if vid.is_file() and vid.suffix.lower() in _VIDEO_EXTS:
                    relpath = str(vid.relative_to(_DOWNLOAD_DIR)).replace("\\", "/")
                    date_str = _format_date(vid.name[:8]) if len(vid.name) >= 8 else ""
                    videos.append({
                        "name": vid.name,
                        "author": entry.name,
                        "relpath": relpath,
                        "size": vid.stat().st_size,
                        "size_fmt": _format_size(vid.stat().st_size),
                        "date": date_str,
                    })

    # Sort by filename descending (YYYYMMDD prefix = newest first)
    videos.sort(key=lambda v: v["name"], reverse=True)
    slides.sort(key=lambda s: s["name"], reverse=True)

    return {
        "videos": videos,
        "slides": slides,
        "empty": not videos and not slides,
    }


# Video extensions browsers can play natively
_VIDEO_EXTS = {".mp4", ".webm"}
# Video extensions that need ffmpeg conversion to .mp4
_VIDEO_CONVERT_EXTS = {".mov", ".mkv", ".avi"}

# ── Dedup state ──────────────────────────────────────────────────────

_IMAGE_EXTS = {".webp", ".jpg", ".jpeg", ".png", ".gif"}
_DEDUP_INDEX: dict[str, tuple[int, bytes]] = {}  # relpath → (dhash, 32×32 thumb bytes)
_PENDING_DUPS: list[dict] = []  # pending duplicate confirmations
_DHASH_THRESHOLD = 5
_MSE_THRESHOLD = 50.0


def _media_to_image(filepath: Path) -> Image.Image:
    """Return an RGB PIL Image for a media file (image or video first frame)."""
    ext = filepath.suffix.lower()
    if ext in _IMAGE_EXTS:
        return Image.open(filepath).convert("RGB")
    # Video: extract first frame via ffmpeg, pipe JPEG to PIL
    proc = subprocess.run([
        "ffmpeg", "-y", "-i", str(filepath),
        "-vframes", "1", "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "3", "-",
    ], check=True, timeout=30, capture_output=True)
    return Image.open(BytesIO(proc.stdout)).convert("RGB")


def _compute_dhash(img: Image.Image) -> int:
    """64-bit difference hash (9×8 grayscale)."""
    gray = img.convert("L").resize((9, 8), Image.LANCZOS)
    pixels = list(gray.get_flattened_data())
    h = 0
    for row in range(8):
        row_off = row * 9
        for col in range(8):
            if pixels[row_off + col] > pixels[row_off + col + 1]:
                h |= 1 << (row * 8 + col)
    return h


def _compute_thumbnail(img: Image.Image) -> bytes:
    """32×32 grayscale raw bytes for MSE comparison."""
    return img.convert("L").resize((32, 32), Image.LANCZOS).tobytes()


def _hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _mse(a: bytes, b: bytes) -> float:
    n = len(a)
    return sum((a[i] - b[i]) ** 2 for i in range(n)) / n


def _build_dedup_index():
    """Scan all media files under downloads/ and build the dedup index."""
    global _DEDUP_INDEX
    _DEDUP_INDEX.clear()
    if not _DOWNLOAD_DIR.is_dir():
        return
    for entry in sorted(_DOWNLOAD_DIR.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        # slides/ contains images, author dirs contain videos
        for f in sorted(entry.iterdir()):
            if not f.is_file():
                continue
            ext = f.suffix.lower()
            if ext not in (_VIDEO_EXTS | _IMAGE_EXTS):
                continue
            try:
                rel = str(f.relative_to(_DOWNLOAD_DIR)).replace("\\", "/")
                img = _media_to_image(f)
                _DEDUP_INDEX[rel] = (_compute_dhash(img), _compute_thumbnail(img))
            except Exception as e:
                log.warning("Dedup index: skipping %s — %s", f, e)
    log.info("Dedup index built: %d files", len(_DEDUP_INDEX))


def _mime_type(filepath: str) -> str:
    """Map file extension to MIME type."""
    ext = Path(filepath).suffix.lower()
    return {
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".webp": "image/webp",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
    }.get(ext, "application/octet-stream")


def _collect_videos(author: str | None = None) -> list[dict]:
    """Collect all videos, optionally filtered by author folder."""
    videos = []
    if not _DOWNLOAD_DIR.is_dir():
        return videos
    for entry in sorted(_DOWNLOAD_DIR.iterdir()):
        if not entry.is_dir() or entry.name == "slides":
            continue
        if author and entry.name != author:
            continue
        for vid in sorted(entry.iterdir()):
            if vid.is_file() and vid.suffix.lower() in _VIDEO_EXTS:
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
    data["upload_success"] = request.args.get("upload_success", "")
    data["upload_error"] = request.args.get("upload_error", "")
    resp = make_response(render_template_string(INDEX_HTML, **data))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


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
                "is_video": f.suffix.lower() in _VIDEO_EXTS,
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


@app.route("/thumb/<path:filepath>")
def thumb(filepath):
    """Serve a JPEG thumbnail for a video. Generated via ffmpeg and cached on disk."""
    safe = _safe_subpath(filepath)
    if not safe.is_file():
        abort(404, "File not found")

    # Cache key: hex hash of relative path
    cache_key = hashlib.sha256(filepath.encode()).hexdigest()[:16]
    _THUMB_CACHE.mkdir(exist_ok=True)
    thumb_path = _THUMB_CACHE / f"{cache_key}.jpg"

    # Regenerate if missing or source is newer
    if not thumb_path.exists() or thumb_path.stat().st_mtime < safe.stat().st_mtime:
        try:
            subprocess.run([
                "ffmpeg", "-y", "-i", str(safe),
                "-vframes", "1",
                "-vf", "scale=180:320:force_original_aspect_ratio=increase,crop=180:320",
                "-f", "mjpeg", "-q:v", "3",
                str(thumb_path),
            ], check=True, timeout=30, capture_output=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            log.error("Thumbnail generation failed for %s: %s", filepath, e)
            abort(500, "Thumbnail generation failed")

    return send_from_directory(str(_THUMB_CACHE), thumb_path.name, mimetype="image/jpeg")


@app.route("/api/delete", methods=["POST"])
def api_delete():
    """Delete a file or directory under downloads. JSON: {"path": "author/..."}."""
    data = request.get_json(silent=True) or {}
    subpath = data.get("path", "").strip()
    if not subpath:
        return {"success": False, "error": "缺少 path 参数"}, 400

    target = _safe_subpath(subpath)
    if not target.exists():
        return {"success": False, "error": "文件或目录不存在"}, 404

    try:
        cleanup_start = target.parent
        if target.is_dir():
            shutil.rmtree(target)
            cleanup_start = target.parent
        else:
            target.unlink()
        removed_dirs = _cleanup_empty_parents(cleanup_start)
        log.info("Deleted: %s", target)
        if removed_dirs:
            log.info("Removed empty parent directories: %s", removed_dirs)

        # Clean up dedup state
        deleted_rel = str(target.relative_to(_DOWNLOAD_DIR)).replace("\\", "/")
        global _DEDUP_INDEX, _PENDING_DUPS
        if target.is_dir():
            prefix = deleted_rel + "/"
            _DEDUP_INDEX = {k: v for k, v in _DEDUP_INDEX.items()
                            if not k.startswith(prefix) and k != deleted_rel}
            _PENDING_DUPS = [d for d in _PENDING_DUPS
                             if d["new_file"] != deleted_rel
                             and not d["new_file"].startswith(prefix)
                             and not d["match_file"].startswith(prefix)]
        else:
            _DEDUP_INDEX.pop(deleted_rel, None)
            _PENDING_DUPS = [d for d in _PENDING_DUPS
                             if d["new_file"] != deleted_rel
                             and d["match_file"] != deleted_rel]

        download_root = _DOWNLOAD_DIR.resolve()
        return {
            "success": True,
            "removed_empty_dirs": [
                str(path.relative_to(download_root)).replace("\\", "/")
                for path in removed_dirs
            ],
        }
    except OSError as e:
        log.error("Failed to delete %s: %s", target, e)
        return {"success": False, "error": str(e)}, 500


def _convert_video(src: Path, dst: Path) -> bool:
    """Convert video to H.264/AAC mp4 via ffmpeg. Returns True on success."""
    try:
        subprocess.run([
            "ffmpeg", "-y", "-i", str(src),
            "-c:v", "libx264", "-c:a", "aac",
            "-movflags", "+faststart",
            str(dst),
        ], check=True, timeout=300, capture_output=True)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log.error("Video conversion failed for %s: %s", src, e)
        return False


def _upload_response(payload: dict, status: int = 200):
    """Return JSON to enhanced clients and redirect native form submissions."""
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return payload, status

    if payload.get("success_count", 0) > 0:
        return redirect(
            url_for(
                "index",
                upload_success=payload.get("message", ""),
                upload_error=payload.get("error", ""),
            ),
            code=303,
        )
    if payload.get("success"):
        return redirect(
            url_for(
                "index",
                upload_success=f"{payload.get('filename', '')} 上传成功",
            ),
            code=303,
        )
    return redirect(
        url_for("index", upload_error=payload.get("error", "上传失败")),
        code=303,
    )


def _process_uploaded_file(file) -> tuple[dict, int]:
    """Validate and save one uploaded file, returning its payload and status."""
    original_name = Path(file.filename).name
    if "." in original_name:
        stem, ext = original_name.rsplit(".", 1)
        ext = "." + ext.lower()
    else:
        stem, ext = original_name, ""

    needs_convert = False
    if ext in _VIDEO_EXTS:
        file_type = "video"
        subdir = "uploads"
        out_ext = ext
    elif ext in _VIDEO_CONVERT_EXTS:
        file_type = "video"
        subdir = "uploads"
        out_ext = ".mp4"
        needs_convert = True
    elif ext in _IMAGE_EXTS:
        file_type = "image"
        subdir = "slides"
        out_ext = ext
    else:
        all_allowed = _VIDEO_EXTS | _VIDEO_CONVERT_EXTS | _IMAGE_EXTS
        return (
            {
                "success": False,
                "original_filename": original_name,
                "error": f"不支持的文件类型 {ext}，仅支持: {', '.join(sorted(all_allowed))}",
            },
            400,
        )

    safe_stem = re.sub(r"[^\w\-.\\u4e00-\\u9fff]", "_", stem).strip("_") or "upload"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    new_name = f"{timestamp}_{safe_stem}{out_ext}"

    dest_dir = _DOWNLOAD_DIR / subdir
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / new_name

    # For conversion: save as temp file first, then convert
    if needs_convert:
        tmp_name = f"{timestamp}_{safe_stem}{ext}"
        tmp_path = dest_dir / tmp_name
    else:
        tmp_path = None

    try:
        save_path = tmp_path if needs_convert else dest
        file.save(str(save_path))
        log.info("Saved upload: %s (%s)", save_path, _format_size(save_path.stat().st_size))

        if needs_convert:
            log.info("Converting %s → %s ...", tmp_path.name, new_name)
            if not _convert_video(tmp_path, dest):
                # Clean up both files on failure
                for p in (tmp_path, dest):
                    try:
                        p.unlink()
                    except OSError:
                        pass
                return (
                    {
                        "success": False,
                        "original_filename": original_name,
                        "error": "视频转码失败，请检查文件格式",
                    },
                    500,
                )
            # Remove the original after successful conversion
            try:
                tmp_path.unlink()
            except OSError:
                pass
            log.info("Conversion complete: %s", new_name)

        relpath = str(dest.relative_to(_DOWNLOAD_DIR)).replace("\\", "/")
        log.info("Uploaded [%s]: %s (%s)", file_type, relpath, _format_size(dest.stat().st_size))

        # ── Dedup check ──
        dup_result = None
        try:
            img = _media_to_image(dest)
            new_dhash = _compute_dhash(img)
            new_thumb = _compute_thumbnail(img)
            for existing_rel, (existing_dhash, existing_thumb) in _DEDUP_INDEX.items():
                if _hamming(new_dhash, existing_dhash) > _DHASH_THRESHOLD:
                    continue
                mse_val = _mse(new_thumb, existing_thumb)
                if mse_val < _MSE_THRESHOLD:
                    similarity = max(0, 100 - int(mse_val / _MSE_THRESHOLD * 100))
                    dup_result = {
                        "duplicate_of": existing_rel,
                        "dhash_dist": _hamming(new_dhash, existing_dhash),
                        "mse": round(mse_val, 1),
                        "similarity_pct": similarity,
                    }
                    _PENDING_DUPS.append({
                        "new_file": relpath,
                        "match_file": existing_rel,
                        "dhash_dist": dup_result["dhash_dist"],
                        "mse": dup_result["mse"],
                        "similarity_pct": similarity,
                    })
                    log.info("Duplicate candidate: %s ≈ %s (dist=%d, mse=%.1f)",
                             relpath, existing_rel, dup_result["dhash_dist"], dup_result["mse"])
                    break
            if not dup_result:
                _DEDUP_INDEX[relpath] = (new_dhash, new_thumb)
        except Exception as e:
            log.warning("Dedup check skipped for %s: %s", relpath, e)

        response = {
            "success": True,
            "original_filename": original_name,
            "filename": new_name,
            "relpath": relpath,
            "size": dest.stat().st_size,
            "size_fmt": _format_size(dest.stat().st_size),
            "type": file_type,
            "converted": needs_convert,
        }
        if dup_result:
            response["duplicate"] = dup_result
        return response, 200
    except OSError as e:
        log.error("Upload failed: %s", e)
        return {
            "success": False,
            "original_filename": original_name,
            "error": str(e),
        }, 500


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """Upload one or more files; images → slides/, videos → uploads/."""
    if "file" not in request.files:
        return _upload_response({"success": False, "error": "缺少 file 参数"}, 400)

    files = [file for file in request.files.getlist("file") if file and file.filename]
    if not files:
        return _upload_response({"success": False, "error": "未选择文件"}, 400)

    processed = [_process_uploaded_file(file) for file in files]
    if len(processed) == 1:
        payload, status = processed[0]
        return _upload_response(payload, status)

    results = [payload for payload, _status in processed]
    success_count = sum(1 for result in results if result.get("success"))
    failed = [result for result in results if not result.get("success")]
    failed_count = len(failed)
    payload = {
        "success": failed_count == 0,
        "files": results,
        "file_count": len(results),
        "success_count": success_count,
        "failed_count": failed_count,
        "message": f"成功上传 {success_count}/{len(results)} 个文件",
    }
    if failed:
        payload["error"] = "；".join(
            f"{result.get('original_filename', '未知文件')}: {result.get('error', '上传失败')}"
            for result in failed
        )

    if not failed:
        status = 200
    elif success_count:
        status = 207
    else:
        status = max(status for _payload, status in processed)
    return _upload_response(payload, status)


@app.route("/api/dups")
def api_list_dups():
    """List pending duplicate confirmations with file metadata."""
    result = []
    for d in _PENDING_DUPS:
        new_info = _file_info(d["new_file"])
        match_info = _file_info(d["match_file"])
        if new_info and match_info:
            result.append({
                "new_file": new_info,
                "match_file": match_info,
                "dhash_dist": d["dhash_dist"],
                "mse": d["mse"],
                "similarity_pct": d["similarity_pct"],
            })
    return result


@app.route("/api/dup/delete", methods=["POST"])
def api_dup_delete():
    """Delete one file from a duplicate pair — path may be new or match."""
    global _PENDING_DUPS, _DEDUP_INDEX

    data = request.get_json(silent=True) or {}
    path = data.get("path", "").strip()
    if not path:
        return {"success": False, "error": "缺少 path 参数"}, 400

    # Find the pending entry by either new_file or match_file
    entry = None
    for d in _PENDING_DUPS:
        if d["new_file"] == path or d["match_file"] == path:
            entry = d
            break
    if not entry:
        return {"success": False, "error": "未找到对应的重复记录"}, 404

    target = _safe_subpath(path)
    if not target.exists():
        return {"success": False, "error": "文件不存在"}, 404

    try:
        target.unlink()
        _cleanup_empty_parents(target.parent)

        # If deleting the match (existing) file, index the new file
        if path == entry["match_file"]:
            new_target = _safe_subpath(entry["new_file"])
            if new_target.exists():
                img = _media_to_image(new_target)
                _DEDUP_INDEX[entry["new_file"]] = (
                    _compute_dhash(img),
                    _compute_thumbnail(img),
                )
                log.info("Dup resolved: deleted match %s, indexed new %s",
                         path, entry["new_file"])
        else:
            log.info("Dup resolved: deleted new %s, kept match %s",
                     path, entry["match_file"])

        _PENDING_DUPS = [d for d in _PENDING_DUPS if d != entry]
        _DEDUP_INDEX.pop(path, None)
        return {"success": True}
    except OSError as e:
        log.error("Dup delete failed: %s", e)
        return {"success": False, "error": str(e)}, 500


@app.route("/api/dup/keep", methods=["POST"])
def api_dup_keep():
    """Mark as not a duplicate — keep file and add to dedup index."""
    data = request.get_json(silent=True) or {}
    path = data.get("path", "").strip()
    if not path:
        return {"success": False, "error": "缺少 path 参数"}, 400

    target = _safe_subpath(path)
    if not target.exists():
        return {"success": False, "error": "文件不存在"}, 404

    try:
        # Add to dedup index
        img = _media_to_image(target)
        _DEDUP_INDEX[path] = (_compute_dhash(img), _compute_thumbnail(img))
        # Remove from pending
        global _PENDING_DUPS
        _PENDING_DUPS = [d for d in _PENDING_DUPS if d["new_file"] != path]
        log.info("Dup-kept: %s → added to index", path)
        return {"success": True}
    except Exception as e:
        log.error("Dup keep failed: %s", e)
        return {"success": False, "error": str(e)}, 500


def _file_info(relpath: str) -> dict | None:
    """Build a small info dict for a file by relative path."""
    target = _DOWNLOAD_DIR / relpath
    try:
        if not target.is_file():
            return None
        st = target.stat()
        return {
            "name": target.name,
            "relpath": relpath,
            "size": st.st_size,
            "size_fmt": _format_size(st.st_size),
            "is_video": target.suffix.lower() in _VIDEO_EXTS,
        }
    except OSError:
        return None


# ── Error handlers ────────────────────────────────────────────────────

@app.errorhandler(403)
def _forbidden(e):
    explanation = "路径包含非法字符或试图访问下载目录以外的文件。"
    suggestion = "请从首页正常浏览，不要手动修改 URL 路径。"
    return render_template_string(
        ERROR_HTML, code=403,
        title="访问被拒绝",
        explanation=explanation,
        suggestion=suggestion,
        detail=str(e),
    ), 403


@app.errorhandler(404)
def _not_found(e):
    explanation = "请求的文件或目录不存在。可能已被移动、删除，或下载尚未完成。"
    suggestion = "返回首页查看当前可用的下载内容。如果文件刚被下载，可能需要等待几秒刷新。"
    return render_template_string(
        ERROR_HTML, code=404,
        title="内容未找到",
        explanation=explanation,
        suggestion=suggestion,
        detail=str(e),
    ), 404


@app.errorhandler(500)
def _server_error(e):
    explanation = "服务器内部错误。可能是文件系统权限问题、配置错误或代码异常。"
    suggestion = "请通过 SSH 查看容器日志：sudo docker logs douyin_file_browser --tail 30"
    return render_template_string(
        ERROR_HTML, code=500,
        title="服务器错误",
        explanation=explanation,
        suggestion=suggestion,
        detail=str(e),
    ), 500


# ── Templates ─────────────────────────────────────────────────────────

# Shared CSS
_COMMON_CSS = """
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
    background: #f5f5f5; color: #333;
    min-height: 100vh; padding: 20px;
  }
  .container { width: 100%; max-width: 100%; margin: 0 auto; padding: 0 16px; }
  h1 { font-size: 22px; font-weight: 600; color: #111; margin-bottom: 4px; }
  .subtitle { font-size: 13px; color: #999; margin-bottom: 24px; }
  .card-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 14px; margin-bottom: 32px;
  }
  .card {
    background: #fff; border-radius: 12px; padding: 20px;
    transition: background 0.15s, transform 0.15s;
    box-shadow: 0 1px 4px rgba(0,0,0,0.06);
    position: relative;
  }
  .card:hover { background: #fafafa; transform: translateY(-1px); }
  .card-inner {
    display: block; text-decoration: none; color: #333;
  }
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
  .del-btn {
    position: absolute; top: 8px; right: 8px;
    width: 28px; height: 28px; border-radius: 50%; border: none;
    background: rgba(0,0,0,0.08); color: #999; font-size: 16px;
    cursor: pointer; transition: all 0.15s; line-height: 1;
    display: flex; align-items: center; justify-content: center;
  }
  .del-btn:hover { background: #fe2c55; color: #fff; }
  .card { position: relative; }
  video:focus, img:focus { outline: none; }
"""

INDEX_HTML = (
    "<!DOCTYPE html>\n"
    '<html lang="zh-CN">\n<head>\n'
    '<meta charset="UTF-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
    "<title>下载浏览 — Douyin Email Bot</title>\n"
    "<style>" + _COMMON_CSS + """
  .section-header {
    display: flex; align-items: center; gap: 8px;
    font-size: 16px; font-weight: 600; color: #888; margin-bottom: 14px;
    padding-bottom: 8px; border-bottom: 1px solid #e8e8e8;
    cursor: pointer; user-select: none; transition: color 0.15s;
  }
  .section-header:hover { color: #555; }
  .section-header .arrow { transition: transform 0.2s; font-size: 12px; display: inline-block; }
  .section-header.collapsed .arrow { transform: rotate(-90deg); }
  .section-count { font-size: 13px; font-weight: 400; color: #bbb; margin-left: auto; }
  .collapsible-body { transition: opacity 0.2s; }
  .collapsible-body.collapsed { display: none; }
  .card-thumb {
    width: 100%; aspect-ratio: 9 / 16; object-fit: cover; border-radius: 8px;
    background: #e8e8e8; margin-bottom: 10px;
  }
  .card .vname { font-size: 14px; color: #333; word-break: break-all; line-height: 1.3; }
  /* ── Pending duplicates ── */
  .dup-section { margin-bottom: 24px; }
  .dup-card {
    display: flex; gap: 16px; align-items: center;
    background: #fff3cd; border-radius: 12px; padding: 16px 20px;
    margin-bottom: 10px; border: 1px solid #ffc107;
    flex-wrap: wrap;
  }
  .dup-compare { display: flex; gap: 20px; align-items: center; flex: 1; min-width: 0; flex-wrap: wrap; }
  .dup-file { text-align: center; min-width: 120px; }
  .dup-file .thumb {
    width: 120px; aspect-ratio: 9 / 16; object-fit: cover; border-radius: 8px;
    background: #e8e8e8; margin-bottom: 6px;
  }
  .dup-file .fname { font-size: 12px; color: #333; word-break: break-all; line-height: 1.3; max-width: 120px; }
  .dup-file .fsize { font-size: 11px; color: #999; }
  .dup-vs { font-size: 24px; color: #ccc; flex-shrink: 0; }
  .dup-info { text-align: center; flex-shrink: 0; }
  .dup-info .pct { font-size: 28px; font-weight: 700; color: #e67e22; }
  .dup-info .label { font-size: 11px; color: #999; }
  .dup-actions { display: flex; gap: 8px; flex-shrink: 0; }
  .dup-actions .keep-btn {
    padding: 8px 16px; border-radius: 6px; border: none; cursor: pointer;
    background: #27ae60; color: #fff; font-size: 13px; transition: opacity 0.15s;
  }
  .dup-actions .keep-btn:hover { opacity: 0.85; }
  .dup-actions .del-btn2 {
    padding: 8px 16px; border-radius: 6px; border: none; cursor: pointer;
    background: #e74c3c; color: #fff; font-size: 13px; transition: opacity 0.15s;
  }
  .dup-actions .del-btn2:hover { opacity: 0.85; }
  .upload-form {
    margin-bottom: 24px; display: flex; gap: 10px; align-items: center;
    flex-wrap: wrap;
  }
  .upload-input {
    position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
    overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap;
    border: 0;
  }
  .upload-submit { background: #25a55a; }
  .upload-submit:disabled { cursor: wait; opacity: 0.55; }
  .upload-status { font-size: 12px; color: #999; word-break: break-all; }
</style>
</head>
<body>
<div class="container">
  <h1>📦 下载浏览</h1>
  <p class="subtitle">Douyin Email Bot — LAN File Browser</p>

  <form id="uploadForm" class="upload-form" action="{{ url_for('api_upload') }}"
        method="post" enctype="multipart/form-data">
    <input class="upload-input" type="file" id="uploadInput" name="file"
           accept="video/*,image/*" multiple required>
    <label class="btn" for="uploadInput">📁 选择文件</label>
    <button class="btn upload-submit" id="uploadSubmit" type="submit">📤 开始上传</button>
    {% if videos %}
    <a class="btn" href="{{ url_for('playlist') }}">▶ 全部播放（随机）</a>
    {% endif %}
    <span id="uploadStatus" class="upload-status">
      {% if upload_success %}✅ {{ upload_success }}
      {% endif %}
      {% if upload_error %}❌ {{ upload_error }}
      {% endif %}
      {% if not upload_success and not upload_error %}就绪{% endif %}
    </span>
  </form>

  <!-- Pending duplicates section (populated by JS) -->
  <div id="dupSection"></div>

  {% if empty %}
  <div class="empty-state">
    <div class="icon">📭</div>
    <p>暂无下载内容</p>
    <p style="font-size:13px;margin-top:8px">发送抖音链接到邮箱，机器人会自动下载</p>
  </div>
  {% endif %}

  {% if videos %}
  <div class="section-header" onclick="toggleSection(this)" title="点击折叠/展开">
    <span class="arrow">▼</span> 📹 视频
    <span class="section-count">{{ videos | length }} 个</span>
  </div>
  <div class="collapsible-body card-grid">
  {% for v in videos %}
    <div class="card">
      <a class="card-inner" href="{{ url_for('view_video', filepath=v.relpath) }}">
        <img class="card-thumb" src="{{ url_for('thumb', filepath=v.relpath) }}" loading="lazy" alt="" width="180" height="320">
        <div class="vname">{{ v.name }}</div>
        <div class="meta" style="margin-top:4px">
          <span class="stat">{{ v.author }}</span>
          <span class="stat">{{ v.size_fmt }}</span>
        </div>
      </a>
      <button class="del-btn" onclick="confirmDelete(event, '{{ v.relpath|e }}', '视频 {{ v.name|e }}')" title="删除">✕</button>
    </div>
  {% endfor %}
  </div>
  {% endif %}

  {% if slides %}
  <div class="section-header" onclick="toggleSection(this)" title="点击折叠/展开"
       style="margin-top:{% if videos %}10{% else %}0{% endif %}px">
    <span class="arrow">▼</span> 🖼️ 图片
    <span class="section-count">{{ slides | length }} 张</span>
  </div>
  <div class="collapsible-body card-grid">
  {% for s in slides %}
    <div class="card">
      <a class="card-inner" href="{{ url_for('raw_file', filepath=s.relpath) }}" target="_blank">
        <img class="card-thumb" src="{{ url_for('raw_file', filepath=s.relpath) }}" loading="lazy" alt="" width="180" height="320">
        <div class="vname">{{ s.name }}</div>
        <div class="meta" style="margin-top:4px">
          <span class="stat">{{ s.date }}</span>
          <span class="stat">{{ s.size_fmt }}</span>
        </div>
      </a>
      <button class="del-btn" onclick="confirmDelete(event, '{{ s.relpath|e }}', '图片 {{ s.name|e }}')" title="删除">✕</button>
    </div>
  {% endfor %}
  </div>
  {% endif %}
</div>

<script>
function toggleSection(header) {
  header.classList.toggle('collapsed');
  header.nextElementSibling.classList.toggle('collapsed');
}
function confirmDelete(event, path, label) {
  event.stopPropagation();
  event.preventDefault();
  if (!confirm('确定删除 ' + label + '？此操作不可撤销。')) return;
  fetch('/api/delete', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path: path})
  }).then(r => r.json()).then(function(data) {
    if (data.success) location.reload();
    else alert('删除失败: ' + (data.error || '未知错误'));
  }).catch(function(e) { alert('请求失败: ' + e.message); });
}
function setUploadStatus(message, background, color) {
  var status = document.getElementById('uploadStatus');
  status.style.display = 'inline-block';
  status.style.padding = '6px 14px';
  status.style.borderRadius = '6px';
  status.style.fontWeight = '600';
  status.textContent = message;
  status.style.background = background;
  status.style.color = color;
}
var uploadForm = document.getElementById('uploadForm');
var uploadInput = document.getElementById('uploadInput');
var uploadSubmit = document.getElementById('uploadSubmit');
uploadInput.addEventListener('change', function() {
  if (uploadInput.files.length) {
    var selection = uploadInput.files.length === 1
      ? uploadInput.files[0].name
      : uploadInput.files.length + ' 个文件';
    setUploadStatus('已选择：' + selection, '#e8f4fd', '#236a96');
  }
});
uploadForm.addEventListener('submit', function(event) {
  if (!uploadInput.files.length) return;
  event.preventDefault();
  uploadSubmit.disabled = true;
  setUploadStatus('⏳ 上传中...', '#fff3cd', '#856404');
  fetch(uploadForm.action, {
    method: 'POST',
    body: new FormData(uploadForm),
    headers: {'X-Requested-With': 'XMLHttpRequest'}
  })
    .then(function(response) {
      return response.json().then(function(data) {
        return {ok: response.ok, data: data};
      });
    })
    .then(function(result) {
      var data = result.data;
      if (data.file_count) {
        if (data.success_count) {
          var batchMessage = '✅ ' + data.message;
          if (data.failed_count) batchMessage += '；失败：' + data.error;
          setUploadStatus(
            batchMessage,
            data.failed_count ? '#fff3cd' : '#d4edda',
            data.failed_count ? '#856404' : '#155724'
          );
          setTimeout(function() { location.reload(); }, 1800);
        } else {
          uploadSubmit.disabled = false;
          uploadInput.value = '';
          setUploadStatus('❌ 上传失败: ' + data.error, '#f8d7da', '#721c24');
        }
        return;
      }
      if (result.ok && data.success) {
        var label = data.type === 'video' ? '视频' : '图片';
        if (data.duplicate) {
          setUploadStatus(
            '⚠️ 重复候选！相似度 ' + data.duplicate.similarity_pct + '%，刷新中...',
            '#fff3cd', '#856404'
          );
        } else {
          setUploadStatus(
            '✅ ' + label + ' ' + data.filename + ' 上传成功！刷新中...',
            '#d4edda', '#155724'
          );
        }
        setTimeout(function() { location.reload(); }, 1500);
      } else {
        uploadSubmit.disabled = false;
        uploadInput.value = '';
        setUploadStatus(
          '❌ 上传失败: ' + (data.error || '未知错误'),
          '#f8d7da', '#721c24'
        );
      }
    })
    .catch(function(err) {
      uploadSubmit.disabled = false;
      uploadInput.value = '';
      setUploadStatus('❌ 请求失败: ' + err.message, '#f8d7da', '#721c24');
    });
});
// ── Pending duplicates ──
function loadDups() {
  fetch('/api/dups')
    .then(function(r) { return r.json(); })
    .then(function(dups) {
      var container = document.getElementById('dupSection');
      container.innerHTML = '';
      if (!dups.length) return;

      var header = document.createElement('div');
      header.className = 'section-header';
      header.title = '点击折叠/展开';
      header.onclick = function() { toggleSection(header); };
      header.innerHTML = '<span class="arrow">▼</span> ⚠️ 待确认重复' +
        '<span class="section-count">' + dups.length + ' 项</span>';
      container.appendChild(header);

      var body = document.createElement('div');
      body.className = 'collapsible-body dup-section';

      dups.forEach(function(d) {
        var card = document.createElement('div');
        card.className = 'dup-card';

        // Compare section
        var compare = document.createElement('div');
        compare.className = 'dup-compare';

        var newFile = document.createElement('div');
        newFile.className = 'dup-file';
        if (d.new_file.is_video) {
          newFile.innerHTML = '<div class="thumb" style="background:#ddd;display:flex;align-items:center;justify-content:center;font-size:32px">🎬</div>';
        } else {
          newFile.innerHTML = '<img class="thumb" src="/raw/' + d.new_file.relpath + '" loading="lazy" width="120" height="213">';
        }
        newFile.innerHTML += '<div class="fname">📥 ' + d.new_file.name + '</div>' +
          '<div class="fsize">' + d.new_file.size_fmt + '</div>';
        compare.appendChild(newFile);

        var vs = document.createElement('div');
        vs.className = 'dup-vs';
        vs.textContent = '≈';
        compare.appendChild(vs);

        var matchFile = document.createElement('div');
        matchFile.className = 'dup-file';
        if (d.match_file.is_video) {
          matchFile.innerHTML = '<div class="thumb" style="background:#ddd;display:flex;align-items:center;justify-content:center;font-size:32px">🎬</div>';
        } else {
          matchFile.innerHTML = '<img class="thumb" src="/raw/' + d.match_file.relpath + '" loading="lazy" width="120" height="213">';
        }
        matchFile.innerHTML += '<div class="fname">📁 ' + d.match_file.name + '</div>' +
          '<div class="fsize">' + d.match_file.size_fmt + '</div>';
        compare.appendChild(matchFile);

        card.appendChild(compare);

        // Info
        var info = document.createElement('div');
        info.className = 'dup-info';
        info.innerHTML = '<div class="pct">' + d.similarity_pct + '%</div>' +
          '<div class="label">相似度 (d=' + d.dhash_dist + ')</div>';
        card.appendChild(info);

        // Actions — closure captures d, no escaping issues
        var actions = document.createElement('div');
        actions.className = 'dup-actions';

        var keepNewBtn = document.createElement('button');
        keepNewBtn.className = 'keep-btn';
        keepNewBtn.textContent = '⭐ 保留新版';
        keepNewBtn.title = '删除旧文件，保留新上传的文件';
        keepNewBtn.onclick = function() { resolveDup(d.match_file.relpath, 'delete', '旧文件 ' + d.match_file.name); };
        actions.appendChild(keepNewBtn);

        var keepOldBtn = document.createElement('button');
        keepOldBtn.className = 'del-btn2';
        keepOldBtn.textContent = '📁 保留旧版';
        keepOldBtn.title = '删除新文件，保留已有的文件';
        keepOldBtn.onclick = function() { resolveDup(d.new_file.relpath, 'delete', '新文件 ' + d.new_file.name); };
        actions.appendChild(keepOldBtn);

        var keepBothBtn = document.createElement('button');
        keepBothBtn.className = 'keep-btn';
        keepBothBtn.style.background = '#3498db';
        keepBothBtn.textContent = '✅ 都保留';
        keepBothBtn.title = '两个文件都保留';
        keepBothBtn.onclick = function() { resolveDup(d.new_file.relpath, 'keep'); };
        actions.appendChild(keepBothBtn);

        card.appendChild(actions);
        body.appendChild(card);
      });

      container.appendChild(body);
    }).catch(function() {});
}
function resolveDup(path, action, label) {
  if (action === 'delete' && !confirm('确定删除' + (label || '这个文件') + '吗？')) return;
  var endpoint = action === 'delete' ? '/api/dup/delete' : '/api/dup/keep';
  fetch(endpoint, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path: path})
  }).then(function(r) { return r.json(); }).then(function(data) {
    if (data.success) location.reload();
    else alert('操作失败: ' + (data.error || '未知错误'));
  }).catch(function(e) { alert('请求失败: ' + e.message); });
}
loadDups();
</script>
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
    margin: 0 auto 16px; box-shadow: 0 4px 20px rgba(0,0,0,0.5);
    position: relative; aspect-ratio: 16 / 9; max-height: 70vh;
    max-width: 1000px;
  }
  .video-wrapper video {
    position: absolute; top: 0; left: 0;
    width: 100%; height: 100%; object-fit: contain;
  }
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
    margin: 0 auto; box-shadow: 0 4px 20px rgba(0,0,0,0.5);
    position: relative; aspect-ratio: 16 / 9; max-height: 60vh;
    max-width: 1000px;
  }
  .video-wrapper video {
    position: absolute; top: 0; left: 0;
    width: 100%; height: 100%; object-fit: contain;
  }
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
      <div class="playlist-item" id="item-{{ loop.index0 }}" onclick="playIndex({{ loop.index0 }}, true)">
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

function playIndex(queueIdx, scroll) {
  currentQueueIdx = queueIdx;
  const v = currentVideo();
  player.src = "{{ url_for('raw_file', filepath='') }}" + v.relpath;
  player.play().catch(function() {});
  updateUI();
  if (scroll) {
    var item = document.getElementById('item-' + currentVideoIndex());
    if (item) item.scrollIntoView({behavior: 'smooth', block: 'nearest'});
  }
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
    "<title>{{ code }} — {{ title }}</title>\n"
    "<style>" + _COMMON_CSS + """
  .error-box { text-align: center; padding: 60px 20px 40px; }
  .error-box .code { font-size: 72px; font-weight: 700; color: #fe2c55; line-height: 1; }
  .error-box .title { font-size: 20px; color: #333; margin: 8px 0 20px; font-weight: 600; }
  .error-box .section {
    max-width: 480px; margin: 0 auto 16px; text-align: left;
    background: #fff; border-radius: 10px; padding: 18px 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
  }
  .error-box .section .label {
    font-size: 11px; font-weight: 700; color: #999; text-transform: uppercase;
    letter-spacing: 1px; margin-bottom: 6px;
  }
  .error-box .section .text { font-size: 14px; color: #555; line-height: 1.7; }
  .error-box .detail {
    font-size: 11px; color: #bbb; margin-top: 16px; font-family: monospace;
    word-break: break-all;
  }
</style>
</head>
<body>
<div class="container">
  <div class="error-box">
    <div class="code">{{ code }}</div>
    <div class="title">{{ title }}</div>

    <div class="section">
      <div class="label">📋 发生了什么</div>
      <div class="text">{{ explanation }}</div>
    </div>

    <div class="section">
      <div class="label">💡 怎么办</div>
      <div class="text">{{ suggestion }}</div>
    </div>

    <a class="btn" href="{{ url_for('index') }}">← 返回首页</a>

    {% if detail %}
    <div class="detail">调试信息：{{ detail }}</div>
    {% endif %}
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

    _build_dedup_index()

    try:
        app.run(host=args.host, port=args.port, debug=args.debug)
    except KeyboardInterrupt:
        log.info("Shutting down")


if __name__ == "__main__":
    main()
