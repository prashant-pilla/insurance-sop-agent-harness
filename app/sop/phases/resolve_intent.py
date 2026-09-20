"""RESOLVE_INTENT: pick the claim using remembered hints; ask only when ambiguous."""

from __future__ import annotations

from typing import Any

from app.sop.phases import PhaseResult, TurnContext
from app.sop.prompts import FALLBACK_RESOLVE_INTENT
from app.sop.state import Directive

DEFAULT_INTENT_PATH = "general_claim_question"


def claim_brief(claim: dict[str, Any]) -> dict[str, str]:
    return {
        "case_id": claim["case_id"],
        "case_type": claim["case_type"],
        "status": claim["status"],
        "created_at": claim["created_at"],
    }


def handle(ctx: TurnContext) -> PhaseResult:
    state, store = ctx.state, ctx.store
    assert state.party_id is not None
    claims = store.list_claims(state.party_id)
    hints = state.memory.case_hints
    matches, applied = store.filter_claims(claims, hints)

    if applied and len(matches) == 1:
        claim = matches[0]
        state.active_case_id = claim["case_id"]
        state.intent_path = state.intent_path or DEFAULT_INTENT_PATH
        ctx.progress = True
        return PhaseResult(
            decision=(
                f"RESOLVE_INTENT: hints {hints.model_dump(exclude_none=True)} matched {claim['case_id']}, "
                f"intent_path={state.intent_path} -> PROCESS_CASE"
            ),
            next_phase="PROCESS_CASE",
        )

    if applied and len(matches) > 1:
        goal = "Disambiguate which of the matching claims the caller means."
        must_ask = "Ask which of the listed claims they mean, describing each by type, date and status."
        candidates, decision = matches, f"RESOLVE_INTENT: hints matched {len(matches)} claims, disambiguating"
    else:
        goal = "Find out which claim the caller needs help with and why."
        must_ask = "Ask what they are calling about today; you may briefly list their claims."
        candidates = claims
        decision = (
            "RESOLVE_INTENT: hints matched no claims, asking" if applied else "RESOLVE_INTENT: no case hints, asking"
        )

    instructions = ["Identity is verified; you may name claims by type, date and status."]
    if ctx.just_verified:
        instructions.insert(0, "Identity was just verified this turn: briefly confirm that first.")
    if applied and not matches:
        instructions.append("Explain that none of their claims match what they described and list what is on file.")
    directive = Directive(
        phase="RESOLVE_INTENT",
        goal=goal,
        facts={
            "policyholder_name": state.policyholder_name,
            "claims": [claim_brief(c) for c in candidates],
            "remembered_hints": hints.model_dump(exclude_none=True),
        },
        must_ask=must_ask,
        instructions=instructions,
        forbidden=["Quoting amounts, denial reasons or documents before a claim is selected."],
        fallback_text=FALLBACK_RESOLVE_INTENT,
    )
    return PhaseResult(decision=decision, directive=directive)
