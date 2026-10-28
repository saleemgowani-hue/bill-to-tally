"""
Public-demo bootstrap (DEMO_MODE only).

On a showcase deployment such as Streamlit Cloud nobody runs START.bat, so the
app has to prepare its own demo: create the tables, seed the demo company once
and offer a one-click sign-in. Does nothing at all outside DEMO_MODE.

Imported lazily by app.py, and left out of customer builds.
"""
from __future__ import annotations

import contextlib
import io
import sys

from utils.config import settings
from utils.logging import get_logger

log = get_logger("demo_bootstrap")


def enabled() -> bool:
    return bool(settings.DEMO_MODE)


def credentials() -> tuple[str, str]:
    import seed_demo
    return seed_demo.DEMO_EMAIL, seed_demo.DEMO_PASSWORD


def ensure_demo_company() -> None:
    """Create the demo company if it is missing. Safe to call on every run."""
    if not enabled():
        return
    import seed_demo
    saved_argv = sys.argv
    sys.argv = ["seed_demo.py"]
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            seed_demo.main()
    except Exception:
        log.exception("Could not prepare the demo company")
    finally:
        sys.argv = saved_argv
