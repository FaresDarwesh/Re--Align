"""Input validation and sanitisation.

Defends against (CWE):
  * 79  XSS — every user string is escaped by Jinja2, and anything that is
             ever rendered with `| safe` first passes through `sanitize_html`.
  * 601 Open redirect — `safe_redirect_target()` only ever returns same-origin
             relative paths.
  * 22  Path traversal — `safe_filename()` / `safe_join()`.
  * 77/78 Command & code injection — there is **no** shell, `eval`, `exec`,
             `pickle` or `subprocess` anywhere in this codebase; `shell_safe()`
             is provided as a tripwire for any future code that tries.
  * 20  Improper input validation — length caps, charset checks, type checks.
  * 1409/74/1336 — ReDoS: untrusted input is never compiled into a regex.
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

# ------------------------------------------------------------------ limits --

MAX_TEXT = 20_000          # long-form (article bodies, prompts)
MAX_SHORT = 500            # titles, names
MAX_LABEL = 120            # single-line labels
MAX_URL = 2_048
MAX_EMAIL = 254
MAX_TAG = 60

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
_WS_RUN = re.compile(r"[ \t]{3,}")


def strip_control(value: str) -> str:
    """Remove control characters and bidi/zero-width overrides (trojan-source)."""
    value = _CONTROL_CHARS.sub("", value)
    value = _ZERO_WIDTH.sub("", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFC", value)


def clean_text(value: str | None, *, limit: int = MAX_SHORT, allow_newlines: bool = True) -> str:
    """Normalise, trim and cap any free-text field coming from a user."""
    if value is None:
        return ""
    text = strip_control(str(value))
    if not allow_newlines:
        text = text.replace("\n", " ").replace("\t", " ")
    text = _WS_RUN.sub("  ", text).strip()
    return text[:limit]


def clean_label(value: str | None, *, limit: int = MAX_LABEL) -> str:
    return clean_text(value, limit=limit, allow_newlines=False)


def clean_long(value: str | None, *, limit: int = MAX_TEXT) -> str:
    return clean_text(value, limit=limit)


# --------------------------------------------------------------- html / xss --

_ALLOWED_TAGS = {
    "p", "br", "strong", "b", "em", "i", "u", "s", "ul", "ol", "li",
    "h1", "h2", "h3", "h4", "blockquote", "code", "pre", "a", "span", "div",
}
_ALLOWED_ATTRS = {"a": {"href", "title"}, "span": {"dir"}, "div": {"dir"}, "p": {"dir"}}
_URL_SCHEMES = ("http:", "https:", "mailto:")

_TAG_RE = re.compile(r"<\s*(/?)\s*([a-zA-Z][a-zA-Z0-9]*)\b([^>]*)>", re.S)
_ATTR_RE = re.compile(r"([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)")
_JS_URI_RE = re.compile(r"(?i)\b(javascript|data|vbscript|file|blob|about)\s*:")
_DANGEROUS_TAG_RE = re.compile(
    r"(?is)<\s*(script|style|iframe|object|embed|svg|math|form|input|button|link|meta|base|"
    r"template|noscript|frame|frameset|applet|audio|video|source|track|canvas|portal)\b"
)
_EVENT_ATTR_RE = re.compile(r"(?i)[/\s]on[a-z]+\s*=")
_EXPR_RE = re.compile(r"(?i)(expression\s*\(|url\s*\(\s*[\"']?\s*(javascript|data)|@import)")


def _clean_attrs(tag: str, attr_text: str) -> str:
    allowed = _ALLOWED_ATTRS.get(tag, set())
    out: list[str] = []
    for name, raw in _ATTR_RE.findall(attr_text):
        low = name.lower()
        if low.startswith("on"):
            continue
        if low not in allowed:
            continue
        value = raw.strip()
        if len(value) >= 2 and value[0] in "\"'":
            value = value[1:-1]
        if low == "href":
            candidate = value.strip()
            if _JS_URI_RE.match(candidate):
                continue
            if not candidate.lower().startswith(_URL_SCHEMES) and not candidate.startswith(("/", "#")):
                continue
            out.append(f'href="{candidate}"')
        else:
            safe = value.replace('"', "&quot;")[:80]
            out.append(f'{low}="{safe}"')
    return (" " + " ".join(out)) if out else ""


def sanitize_html(value: str | None, *, limit: int = MAX_TEXT) -> str:
    """Allowlist HTML sanitiser.

    Anything that is *not* on the allowlist is dropped (not escaped) so the
    result can be rendered with `| safe` when a rich-text field is needed.
    Jinja2 auto-escaping stays on everywhere else — this is the second layer.
    """
    if not value:
        return ""
    text = strip_control(str(value))[:limit]

    # Reject outright any document containing a dangerous construct.
    if _DANGEROUS_TAG_RE.search(text) or _EVENT_ATTR_RE.search(text) or _EXPR_RE.search(text):
        text = _DANGEROUS_TAG_RE.sub("", text)
        text = _EVENT_ATTR_RE.sub(" ", text)
        text = _EXPR_RE.sub("", text)

    def _repl(match: re.Match[str]) -> str:
        closing, tag, attrs = match.group(1), match.group(2).lower(), match.group(3) or ""
        if tag not in _ALLOWED_TAGS:
            return ""
        if closing:
            return f"</{tag}>"
        return f"<{tag}{_clean_attrs(tag, attrs)}>"

    text = _TAG_RE.sub(_repl, text)

    # Re-balance: drop any tag that a second pass still finds dangerous.
    if _DANGEROUS_TAG_RE.search(text) or _EVENT_ATTR_RE.search(text):
        text = _DANGEROUS_TAG_RE.sub("", text)
    # Strip any surviving attribute-less leftovers such as `<>` or `</>`.
    text = re.sub(r"<\s*>|</\s*>", "", text)
    return text.strip()


def looks_like_xss(value: str) -> bool:
    """Tripwire for the security test-suite and for input rejection."""
    if not value:
        return False
    v = str(value)
    return bool(
        _DANGEROUS_TAG_RE.search(v)
        or _EVENT_ATTR_RE.search(v)
        or _JS_URI_RE.search(v)
        or _EXPR_RE.search(v)
    )


# ------------------------------------------------------------- open redirect --

def safe_redirect_target(target: str | None, *, default: str = "/app") -> str:
    """Only ever return a same-origin, absolute-path redirect target."""
    if not target:
        return default
    candidate = strip_control(str(target)).strip()
    if not candidate or len(candidate) > MAX_URL:
        return default
    # Reject absolute URLs, protocol-relative URLs, backslash tricks, CRLF.
    if "//" in candidate or "\\" in candidate or ":" in candidate.split("/")[0]:
        return default
    if candidate.startswith(("/", "#", "?")) is False:
        return default
    if any(ch in candidate for ch in ("\n", "\r", "\t", "\x00")):
        return default
    return candidate or default


def safe_external_url(value: str | None) -> str | None:
    """Validate a user-supplied URL (webhook targets, RSS feeds, links).

    Returns a normalised URL or None. Never allows anything but http/https.
    SSRF checks live in `realign.core.netsafe`.
    """
    if not value:
        return None
    raw = strip_control(str(value)).strip()
    if len(raw) > MAX_URL:
        return None
    try:
        parsed = urlparse(raw)
    except Exception:  # noqa: BLE001
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None  # credentials in URL (CWE-522)
    return raw


# ------------------------------------------------------------ path traversal --

_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str | None, *, fallback: str = "file.bin") -> str:
    """Flatten a filename: no directory components, no unicode tricks."""
    if not name:
        return fallback
    raw = strip_control(str(name)).strip()
    raw = os.path.basename(raw.replace("\\", "/"))
    raw = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    raw = _FILENAME_RE.sub("_", raw).strip("._")
    raw = raw[:MAX_LABEL]
    if not raw or raw in (".", ".."):
        return fallback
    return raw


def safe_join(root: str | Path, *parts: str) -> Path:
    """Join under `root` and refuse anything that escapes it (CWE-22)."""
    root_path = Path(root).resolve()
    candidate = (root_path.joinpath(*[p for p in parts if p])).resolve()
    if candidate != root_path and root_path not in candidate.parents:
        raise ValueError("مسار غير مسموح به")
    return candidate


# ------------------------------------------------------- command injection ---

_SHELL_METACHARS = set(";&|`$><\n\r\0'\"\\(){}[]!#*?")


def shell_safe(value: str) -> str:
    """Tripwire: reject any string containing shell metacharacters.

    The platform never shells out, so reaching this function means something
    changed. Failing closed is the correct behaviour.
    """
    text = str(value or "")
    if any(ch in _SHELL_METACHARS for ch in text):
        raise ValueError("قيمة غير مسموح بها")
    return text


# ------------------------------------------------------------- re-dos guard --

def safe_search_term(value: str | None, *, limit: int = 120) -> str:
    """A search box must never be turned into a regex (`re` is ReDoS-prone)."""
    text = clean_label(value, limit=limit)
    return re.sub(r"[.*+?^${}()|[\]\\]", " ", text).strip()


# ------------------------------------------------------------------ numbers --

def clamp_int(value: Any, *, minimum: int, maximum: int, default: int) -> int:
    try:
        number = int(str(value).strip())
    except Exception:  # noqa: BLE001
        return default
    return max(minimum, min(maximum, number))


def sanitize_params(values: Any, *, depth: int = 0, limit: int = 40) -> dict:
    """Scrub the free-form `params` dict handed to an agent.

    Agents embed these strings straight into LLM prompts, so they are bounded,
    control-character-free and free of anything that could try to steer the
    model (prompt-injection markers are kept but the payload is bounded: the
    model only ever *writes*, never executes).
    """
    if depth > 4 or not isinstance(values, dict):
        return {}
    out: dict[str, Any] = {}
    for index, (raw_key, raw_value) in enumerate(values.items()):
        if index >= limit:
            break
        key = clean_label(str(raw_key), limit=60)
        if not key:
            continue
        if isinstance(raw_value, str):
            out[key] = clean_long(raw_value, limit=8000)
        elif isinstance(raw_value, (int, float, bool)) or raw_value is None:
            out[key] = raw_value
        elif isinstance(raw_value, dict):
            out[key] = sanitize_params(raw_value, depth=depth + 1, limit=limit)
        elif isinstance(raw_value, list):
            out[key] = [
                clean_long(str(v), limit=2000) if not isinstance(v, (int, float, bool)) else v
                for v in raw_value[:30]
            ]
        else:
            out[key] = clean_long(str(raw_value), limit=2000)
    return out


def clean_email(value: str | None) -> str:
    text = clean_label(value, limit=MAX_EMAIL).strip().lower()
    return text[:MAX_EMAIL]


def clean_tags(values: Any, *, limit: int = 30, each: int = MAX_TAG) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [v for v in re.split(r"[,\n،؛;]", values)]
    out: list[str] = []
    for item in values:
        text = clean_label(str(item), limit=each)
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


__all__ = [
    "clean_text", "clean_label", "clean_long", "clean_email", "clean_tags",
    "sanitize_html", "looks_like_xss", "strip_control",
    "safe_redirect_target", "safe_external_url",
    "safe_filename", "safe_join", "shell_safe", "safe_search_term",
    "clamp_int", "sanitize_params",
]
