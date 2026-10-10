"""Secure file uploads (CWE-434, CWE-22, CWE-611, CWE-400).

Rules enforced here:
  1. allowlist of MIME types *confirmed by magic bytes* — never trust the
     `Content-Type` header or the file extension, both are attacker-controlled
  2. hard size cap (before and after read)
  3. the image is **decoded and re-encoded** by Pillow, which strips any
     embedded payload (polyglot files, EXE-in-PNG, PHP-in-JPEG, EXIF scripts)
  4. filename is discarded; a random name is generated (no traversal, no
     unicode right-to-left override, no double extension `logo.php.png`)
  5. files are written outside the web root and served by a controlled
     endpoint with a locked-down content type (`X-Content-Type-Options` +
     `Content-Disposition: inline` + `Content-Security-Policy` from the base)
  6. SVG is refused outright — it is an XML document and a script carrier
"""

from __future__ import annotations

import io
import secrets
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from realign.config import settings
from realign.core.utils import utcnow

# Magic-byte signatures for the allowlist.
_SIGNATURES: dict[str, list[bytes]] = {
    "image/png": [b"\x89PNG\r\n\x1a\n"],
    "image/jpeg": [b"\xff\xd8\xff"],
    "image/webp": [b"RIFF"],
}

MAX_DIMENSION = 4096
MAX_BYTES = 2 * 1024 * 1024


class UploadRejected(Exception):
    pass


@dataclass(slots=True)
class StoredFile:
    path: Path
    filename: str
    content_type: str
    size: int
    width: int
    height: int

    @property
    def url(self) -> str:
        return f"/media/{self.filename}"


def _sniff(data: bytes) -> str | None:
    if data[:2] == b"MZ" or data[:4] in (b"\x7fELF", b"%PDF") or data[:5] == b"<?xml" or b"<svg" in data[:2048]:
        return None  # executable / polyglot / SVG
    for mime, signatures in _SIGNATURES.items():
        for signature in signatures:
            if data.startswith(signature):
                if mime == "image/webp" and data[8:12] != b"WEBP":
                    return None
                return mime
    return None


def _media_root() -> Path:
    root = Path(settings.media_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def store_image(data: bytes, *, original_name: str = "", subdir: str = "logo") -> StoredFile:
    if not data:
        raise UploadRejected("الملف فارغ")
    if len(data) > MAX_BYTES or len(data) > settings.upload_max_bytes:
        raise UploadRejected("حجم الصورة أكبر من المسموح (٢ ميجابايت)")

    detected = _sniff(data)
    if detected is None:
        raise UploadRejected("نوع الملف غير مدعوم — المسموح PNG و JPG و WebP فقط")
    if detected not in settings.upload_allowed_type_list:
        raise UploadRejected("نوع الملف غير مسموح به")

    # Decode → re-encode. Anything smuggled inside the file is destroyed here.
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:  # noqa: BLE001
        raise UploadRejected("الصورة تالفة أو غير صالحة") from exc

    if image.width > MAX_DIMENSION or image.height > MAX_DIMENSION:
        raise UploadRejected("أبعاد الصورة أكبر من المسموح")

    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA" if image.mode == "P" else "RGB")
    if image.mode == "RGBA":
        image = image.convert("RGB")

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    cleaned = buffer.getvalue()
    if len(cleaned) > MAX_BYTES:
        raise UploadRejected("حجم الصورة بعد المعالجة أكبر من المسموح")

    target_dir = _media_root() / subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%d")
    filename = f"{stamp}-{secrets.token_hex(16)}.png"
    path = target_dir / filename
    path.write_bytes(cleaned)
    try:
        path.chmod(0o640)
    except OSError:  # noqa: BLE001 - not fatal on every filesystem
        pass

    return StoredFile(
        path=path,
        filename=f"{subdir}/{filename}",
        content_type="image/png",
        size=len(cleaned),
        width=image.width,
        height=image.height,
    )


def resolve_media(relative: str) -> Path | None:
    """Map `/media/<subdir>/<file>` back to a path, refusing traversal."""
    cleaned = (relative or "").strip().lstrip("/")
    if not cleaned or ".." in cleaned or cleaned.startswith("/"):
        return None
    if "/" not in cleaned:
        return None
    subdir, _, name = cleaned.partition("/")
    if subdir not in {"logo", "brand", "avatar"}:
        return None
    if "/" in name or "\\" in name or not name.endswith(".png"):
        return None
    candidate = (_media_root() / subdir / name).resolve()
    root = _media_root().resolve()
    if root not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


__all__ = ["store_image", "resolve_media", "UploadRejected", "StoredFile", "MAX_BYTES"]
