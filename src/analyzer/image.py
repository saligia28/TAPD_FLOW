from __future__ import annotations

import hashlib
import io
import mimetypes
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse

import requests

try:
    from PIL import Image  # type: ignore
except Exception:  # pragma: no cover
    Image = None  # type: ignore


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_DIR = REPO_ROOT.parent / "data" / "images"


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except Exception:
        return default


def _env_bool(key: str, default: bool = True) -> bool:
    v = os.getenv(key, "").strip().lower()
    if not v:
        return default
    return v in {"1", "true", "yes", "on"}


def analyze_images(
    urls: List[str],
    *,
    story_id: Optional[str] = None,
    cache_dir: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Light-weight image analysis (no ML):

    - Fetch headers/body (size-capped)
    - Extract content-type, format, width/height via Pillow (if available)
    - Compute a simple dominant color (avg RGB) as hint
    Returns list of {url, ok, reason?, content_type?, format?, width?, height?, avg_color?}

    Env controls:
    - IMAGE_ANALYSIS: 1/0 (default 1)
    - IMAGE_ANALYSIS_MAX: max images to analyze (default 3)
    - IMAGE_ANALYSIS_TIMEOUT: seconds per request (default 6)
    - IMAGE_ANALYSIS_MAX_BYTES: max bytes to read (default 2_000_000)
    """
    if not _env_bool("IMAGE_ANALYSIS", True):
        return []
    max_images = _env_int("IMAGE_ANALYSIS_MAX", 3)
    timeout = _env_int("IMAGE_ANALYSIS_TIMEOUT", 6)
    max_bytes = _env_int("IMAGE_ANALYSIS_MAX_BYTES", 2_000_000)

    cache_root = Path(cache_dir) if cache_dir else Path(os.getenv("IMAGE_CACHE_DIR", DEFAULT_CACHE_DIR))
    out: List[Dict[str, Any]] = []
    for url in urls[:max_images]:
        rec: Dict[str, Any] = {"url": url, "ok": False}
        try:
            if not (url.startswith("http://") or url.startswith("https://")):
                rec["reason"] = "non-http url"
                out.append(rec)
                continue
            # Try to fetch limited bytes
            r = requests.get(url, stream=True, timeout=timeout)
            r.raise_for_status()
            ctype = r.headers.get("Content-Type", "")
            rec["content_type"] = ctype
            # Read up to max_bytes
            buf = io.BytesIO()
            size = 0
            for chunk in r.iter_content(8192):
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    break
                buf.write(chunk)
            data = buf.getvalue()
            rec["size_bytes"] = len(data)
            if Image:
                try:
                    img = Image.open(io.BytesIO(data))
                    rec["format"] = img.format
                    rec["width"], rec["height"] = img.size
                    # avg color
                    thumb = img.convert("RGB")
                    thumb = thumb.resize((16, 16))
                    pixels = list(thumb.getdata())
                    n = len(pixels)
                    if n:
                        rsum = sum(p[0] for p in pixels)
                        gsum = sum(p[1] for p in pixels)
                        bsum = sum(p[2] for p in pixels)
                        rec["avg_color"] = (rsum // n, gsum // n, bsum // n)
                except Exception:  # image parse failure
                    pass
            saved_path = _maybe_persist_image(data, url, cache_root, story_id, ctype)
            if saved_path:
                rec["saved_path"] = saved_path
            rec.update(_enrich_description(rec))
            rec["ok"] = True
        except Exception as e:
            rec["reason"] = str(e)
            rec.update(_enrich_description(rec))
        out.append(rec)
    return out


def _maybe_persist_image(
    data: bytes,
    url: str,
    cache_root: Path,
    story_id: Optional[str],
    content_type: str,
) -> Optional[str]:
    if not data:
        return None
    try:
        rel_dir = story_id or _short_hash(url)
        target_dir = cache_root / rel_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        filename = _derive_filename(url, content_type)
        path = target_dir / filename
        if not path.exists():
            with open(path, "wb") as fh:
                fh.write(data)
        try:
            return str(path.relative_to(REPO_ROOT.parent))
        except ValueError:
            return str(path)
    except Exception:  # pragma: no cover - filesystem issues
        return None


def _derive_filename(url: str, content_type: str) -> str:
    parsed = urlparse(url)
    name = os.path.basename(parsed.path)
    if name:
        name = unquote(name)
    if not name:
        name = _short_hash(url)
    if "." not in name:
        ext = ""
        if content_type:
            ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ""
        if not ext:
            ext = ".img"
        name = f"{name}{ext}"
    return name


def _short_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]


def _enrich_description(rec: Dict[str, Any]) -> Dict[str, Any]:
    parts: List[str] = []
    if rec.get("format") and rec.get("width") and rec.get("height"):
        parts.append(f"{rec['format']} {rec['width']}x{rec['height']}")
    if rec.get("size_bytes"):
        parts.append(_human_readable_size(int(rec["size_bytes"])))
    if rec.get("avg_color") and isinstance(rec["avg_color"], tuple):
        r, g, b = rec["avg_color"]
        parts.append(f"主色 #{r:02X}{g:02X}{b:02X}")
    if rec.get("saved_path"):
        parts.append(f"已保存: {rec['saved_path']}")
    if rec.get("reason") and not rec.get("ok"):
        parts.append(f"失败: {rec['reason']}")
    if parts:
        rec["description"] = " | ".join(parts)
    return rec


def _human_readable_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{size}B"
