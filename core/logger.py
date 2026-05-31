"""
core/logger.py — Structured Rotating Logger for Jarvis MK37.

Concept: Enterprise logging has two output channels running simultaneously:
  1. Console   — human-readable, INFO+ only, shown in terminal
  2. Rotating file — full DEBUG+, machine-readable, 10MB × 5 files

Correlation IDs trace a complete voice interaction (wake-word → response)
across every module that handles it. Set a new ID at the start of each turn,
and every log line in that turn carries the same ID for easy grep/filtering.

Usage:
    from core.logger import get_logger
    log = get_logger(__name__)
    log.info("Processing user command")

One-time setup (main.py, before any other imports):
    from core.logger import setup_logging
    setup_logging()
"""

import logging
import logging.handlers
import threading
import uuid
from pathlib import Path

# ── Paths ───────────────────────────────────────────────────────
_BASE_DIR  = Path(__file__).parent.parent
_LOG_DIR   = _BASE_DIR / "logs"
_LOG_FILE  = _LOG_DIR / "jarvis.log"

# ── Init guard ──────────────────────────────────────────────────
_initialized    = False
_init_lock      = threading.Lock()

# ── Per-turn correlation ID ─────────────────────────────────────
_correlation_id  = "-"
_correlation_lock = threading.Lock()


def new_turn_id() -> str:
    """Generate a fresh correlation ID and make it current. Call at wake-word."""
    cid = uuid.uuid4().hex[:8]
    with _correlation_lock:
        global _correlation_id
        _correlation_id = cid
    return cid


def clear_turn_id() -> None:
    """Clear correlation ID after a turn completes."""
    with _correlation_lock:
        global _correlation_id
        _correlation_id = "-"


def get_turn_id() -> str:
    with _correlation_lock:
        return _correlation_id


# ── Log filter that stamps every record with the correlation ID ─
class _CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.cid = get_turn_id()
        return True


# ── Public setup ────────────────────────────────────────────────

def setup_logging(console_level: int = logging.INFO) -> None:
    """
    One-time global logging setup. Call this as the FIRST line of main().
    Subsequent calls are no-ops (idempotent).

    Args:
        console_level: Minimum severity shown in terminal (default INFO).
                       File always captures DEBUG.
    """
    global _initialized
    with _init_lock:
        if _initialized:
            return
        _initialized = True

    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # capture everything; handlers filter

    fmt = logging.Formatter(
        "%(asctime)s [%(cid)s] %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    corr = _CorrelationFilter()

    # ── Rotating file: 10 MB × 5 backup files ──────────────────
    fh = logging.handlers.RotatingFileHandler(
        _LOG_FILE,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    fh.addFilter(corr)

    # ── Console: INFO+ only (keeps terminal readable) ───────────
    ch = logging.StreamHandler()
    ch.setLevel(console_level)
    ch.setFormatter(fmt)
    ch.addFilter(corr)

    root.addHandler(fh)
    root.addHandler(ch)

    # Silence noisy third-party libraries
    for noisy in ("httpx", "httpcore", "urllib3", "transformers",
                  "torch", "PIL", "vosk"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        "Logging initialised → %s (console=%s)",
        _LOG_FILE,
        logging.getLevelName(console_level),
    )


def get_logger(name: str) -> logging.Logger:
    """
    Convenience wrapper — use instead of logging.getLogger().

    Example:
        log = get_logger(__name__)
        log.info("hello")
    """
    return logging.getLogger(name)
