"""Vercel / serverless entrypoint.

Vercel's Python runtime imports this module and looks for an ASGI `app`.
Everything else (routing, templates, static files) lives in the `realign`
package, so there is no duplicated logic here on purpose.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Vercel sets VERCEL=1 itself; the app detects it and switches to
# serverless mode (inline run execution, no background worker).
os.environ.setdefault("VERCEL", "1")

from realign.main import app  # noqa: E402  (must come after sys.path fix)

__all__ = ["app"]
