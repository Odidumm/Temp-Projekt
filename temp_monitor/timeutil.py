from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime | None) -> str | None:
    """ISO-8601 in UTC mit ``Z``, z. B. ``2026-11-22T13:05:54Z``."""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def clock_synchronized() -> bool:
    """True, wenn die Systemuhr laut systemd per NTP gestellt ist."""
    try:
        result = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("NTP-Status nicht lesbar: %s", exc)
        return False
    return result.stdout.strip() == "yes"
