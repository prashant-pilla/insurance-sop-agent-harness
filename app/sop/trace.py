"""Opt-in per-turn debug trace written as JSONL, one file per session."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class Tracer:
    def __init__(self, enabled: bool, directory: Path):
        self.enabled = enabled
        self._directory = directory

    def record(self, session_id: str, turn: int, phase_before: str, phase_after: str, debug: dict[str, Any]) -> None:
        if not self.enabled:
            return
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "session_id": session_id,
            "turn": turn,
            "phase_before": phase_before,
            "phase_after": phase_after,
            "debug": debug,
        }
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            with (self._directory / f"{session_id}.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, default=str) + "\n")
        except OSError as exc:
            logger.warning("trace write failed: %s", exc)
