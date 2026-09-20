"""HUMAN_HANDOFF: terminal mock transfer with a note for the human representative."""

from __future__ import annotations

from app.sop.phases import PhaseResult, TurnContext
from app.sop.prompts import FALLBACK_HANDOFF
from app.sop.state import Directive, SessionState


def build_handoff_note(state: SessionState, reason: str, emotion: str | None = None) -> str:
    hints = state.memory.case_hints.model_dump(exclude_none=True)
    parts = [
        f"Reason: {reason}",
        f"Identity verified: {'yes' if state.verified else 'no'}"
        + (f" ({state.policyholder_name})" if state.verified and state.policyholder_name else ""),
        f"Active claim: {state.active_case_id or 'none'}",
        f"Intent: {state.intent_path or 'unknown'}",
        f"Intent hints: {', '.join(state.memory.intent_hints) or 'none'}",
        f"Case hints: {hints or 'none'}",
        f"Last emotion: {emotion or state.last_emotion or 'unknown'}",
    ]
    rep = state.representative
    if state.caller_role == "representative" and rep is not None:
        who = f"representative {rep.name or 'unknown'}" + (f" ({rep.relationship})" if rep.relationship else "")
        parts.append(
            f"Caller: {who} for {rep.on_behalf_of or 'unknown policyholder'}; consent: {state.consent_status or 'not requested'}"
        )
    return "; ".join(parts)


def handle(ctx: TurnContext) -> PhaseResult:
    state = ctx.state
    state.phase = "HUMAN_HANDOFF"
    state.handoff_note = build_handoff_note(state, ctx.handoff_reason or "handoff", ctx.extraction.emotion)
    instructions = [
        "Acknowledge the caller's situation in one sentence.",
        "Tell them you are connecting them with a human claims representative who will pick up from here.",
        "Do not ask any further questions.",
    ]
    if not state.verified:
        instructions.append("Identity is not verified: do not mention any claim information.")
    directive = Directive(
        phase="HUMAN_HANDOFF",
        goal="Transfer the caller to a human claims representative.",
        facts={"reason": ctx.handoff_reason, "verified": state.verified},
        instructions=instructions,
        forbidden=["Claim details when unverified.", "Asking questions."],
        fallback_text=FALLBACK_HANDOFF,
    )
    return PhaseResult(decision=f"HUMAN_HANDOFF: {ctx.handoff_reason}", directive=directive)
