from __future__ import annotations
import io
import os
from typing import Any, Dict, List

import requests

try:
    from PIL import Image  # type: ignore
except Exception:  # pragma: no cover
    Image = None  # type: ignore


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


def analyze_images(urls: List[str]) -> List[Dict[str, Any]]:
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
            rec["ok"] = True
        except Exception as e:
            rec["reason"] = str(e)
        out.append(rec)
    return out

