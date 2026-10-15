"""A daily cap on Gemini tokens, so a loop, a flood of captures or a crafted payload can't run up the bill.
Counted per instance (Cloud Run runs at most two); when it is reached, Gemini calls stop until the next
UTC day and a warning goes to Cloud Logging."""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone

from .audit import audit

DAILY_TOKENS = int(os.environ.get("DAILY_TOKEN_BUDGET", "3000000"))
_lock = threading.Lock()
_state = {"day": "", "used": 0, "warned": False}


class BudgetExceeded(RuntimeError):
    """Today's Gemini budget is spent."""


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _roll() -> None:
    if _state["day"] != _today():
        _state.update(day=_today(), used=0, warned=False)


def spend_check() -> None:
    with _lock:
        _roll()
        if _state["used"] >= DAILY_TOKENS:
            if not _state["warned"]:
                _state["warned"] = True
                audit("token_budget_reached", "WARNING", used=_state["used"], budget=DAILY_TOKENS)
            raise BudgetExceeded(f"daily Gemini budget of {DAILY_TOKENS} tokens reached")


def spend(tokens: int) -> None:
    with _lock:
        _roll()
        _state["used"] += tokens


def used_today() -> int:
    with _lock:
        _roll()
        return _state["used"]
