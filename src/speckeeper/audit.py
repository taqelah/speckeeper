"""Structured audit events for Cloud Logging: who approved or dismissed what, refused clicks, bad
signatures and secrets, budget and rate limits. Cloud Run turns a JSON line on stdout into a log entry
with that severity, so these can be filtered and alerted on (jsonPayload.audit.event)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone


def audit(event: str, severity: str = "NOTICE", **fields) -> None:
    entry = {
        "severity": severity,
        "message": f"audit {event}",
        "audit": {"event": event, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **fields},
    }
    print(json.dumps(entry, default=str), file=sys.stdout, flush=True)
