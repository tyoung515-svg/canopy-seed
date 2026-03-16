"""
Canopy Seed Logger
------------------
Centralized logging configuration for the entire application.
Handles rotation, formatting, and console/file outputs.
API key redaction ensures secrets never appear in log files.
"""

import logging
import re
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler

# Default log settings
LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "canopy.log"
MAX_BYTES = 5 * 1024 * 1024  # 5 MB
backup_count = 3

# ── API key redaction ────────────────────────────────────────────────────────
# Matches ?key=AIza... or &key=AIza... (Google AI API key in URL query params)
# Also catches generic Bearer tokens and x-goog-api-key headers logged by httpx.
_KEY_PATTERNS = [
    (re.compile(r'([?&])key=[A-Za-z0-9_-]{20,}'), r'\1key=REDACTED'),
    (re.compile(r'(x-goog-api-key:\s*)[A-Za-z0-9_-]{20,}', re.IGNORECASE), r'\1REDACTED'),
    (re.compile(r'(Bearer\s+)[A-Za-z0-9_.-]{20,}', re.IGNORECASE), r'\1REDACTED'),
]


class _KeyRedactingFilter(logging.Filter):
    """Scrub API keys / bearer tokens from log records before they hit any handler."""
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for pattern, replacement in _KEY_PATTERNS:
            msg = pattern.sub(replacement, msg)
        # Overwrite args so the formatted message uses the redacted version
        record.msg = msg
        record.args = None
        return True


def setup_logging(level=logging.INFO):
    """
    Configures the root logger to write to both console and file.
    Creates the 'logs/' directory if it doesn't exist.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Calculate log format
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 1. File Handler (Rotating)
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=MAX_BYTES, backupCount=backup_count, encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)

    # 2. Console Handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)

    # Root Logger Config
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # clear existing handlers to avoid duplicates
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    # Attach the key-redacting filter to the root logger so it covers ALL handlers
    # (remove duplicates first in case setup_logging is called twice)
    for f in root_logger.filters[:]:
        if isinstance(f, _KeyRedactingFilter):
            root_logger.removeFilter(f)
    root_logger.addFilter(_KeyRedactingFilter())

    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    # Suppress noisy httpx INFO logs (they log every HTTP request URL, which
    # previously leaked API keys even after redaction — belt and suspenders)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    logging.info(f"Logging initialized. Writing to: {LOG_FILE}")

def get_logger(name: str) -> logging.Logger:
    """Helper to get a logger ensuring setup is active."""
    return logging.getLogger(name)

if __name__ == "__main__":
    setup_logging()
    logging.info("Test log entry.")
