"""Rotating file + console logging for the whole application."""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from utils.config import settings

_CONFIGURED = False
_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def configure_logging(level: int = logging.INFO) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    Path(settings.LOG_DIR).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("billtotally")
    root.setLevel(level)
    root.propagate = False

    file_handler = logging.handlers.RotatingFileHandler(
        Path(settings.LOG_DIR) / "app.log",
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(console)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(f"billtotally.{name}")
