"""Mock email delivery: entries land in the session outbox instead of being sent."""

from __future__ import annotations

from datetime import datetime, timezone

from app.sop.state import OutboxEntry, SessionState


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}*****@{domain}"


def send_email(state: SessionState, to: str, subject: str, body: str) -> OutboxEntry:
    entry = OutboxEntry(
        to_masked=mask_email(to),
        subject=subject,
        body=body,
        sent_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    state.outbox.append(entry)
    return entry
