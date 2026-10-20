"""Read the text inside note images, locally (RapidOCR, CPU)."""
from typing import Callable

import httpx

FAILED = "图片文字未识别"
_engine = None


def _default_engine():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR()
    return _engine


def _fetch(url: str) -> bytes:
    return httpx.get(url, timeout=15, follow_redirects=True).content


def image_text(urls: list[str], max_images: int = 6, fetch: Callable[[str], bytes] | None = None,
               engine=None) -> str:
    if not urls:
        return ""
    fetch = fetch or _fetch
    engine = engine or _default_engine()
    blocks, failures = [], 0
    for n, url in enumerate(urls[:max_images], start=1):
        try:
            result, _ = engine(fetch(url))
        except Exception:
            failures += 1
            continue
        if not result:
            continue
        lines = [r[1] for r in sorted(result, key=lambda r: r[0][0][1])]
        blocks.append(f"[图 {n}]\n" + "\n".join(lines))
    if not blocks and failures:
        return FAILED
    return "\n".join(blocks)
