"""PROCESS_CASE: grounded answers from the claim record and fixture guidance."""

from __future__ import annotations

from app.sop.phases import PhaseResult, TurnContext
from app.sop.phases.resolve_intent import claim_brief
from app.sop.prompts import FALLBACK_PROCESS_CASE
from app.sop.state import Directive


def handle(ctx: TurnContext) -> PhaseResult:
    state, store, extraction = ctx.state, ctx.store, ctx.extraction
    assert state.party_id is not None
    if state.active_case_id is None:
        return PhaseResult(decision="PROCESS_CASE: no active claim -> RESOLVE_INTENT", next_phase="RESOLVE_INTENT")

    claims = store.list_claims(state.party_id)
    switched = _maybe_switch_claim(ctx, claims)
    claim = store.get_claim(state.active_case_id)
    assert claim is not None

    if extraction.conversation_done and not ctx.entered_this_turn:
        return PhaseResult(decision="PROCESS_CASE: conversation_done -> POST_PROCESS", next_phase="POST_PROCESS")

    if extraction.scope == "in_scope_claim":
        ctx.progress = True

    facts = store.guidance_for_claim(claim, state.intent_path, ctx.message)
    facts["policyholder_name"] = state.policyholder_name
    holder = store.get_policyholder(state.party_id)
    facts["policy_number_on_file"] = holder["policy_number"] if holder else None
    facts["intent_path"] = state.intent_path
    facts["other_claims"] = [claim_brief(c) for c in claims if c["case_id"] != claim["case_id"]]

    instructions = [
        "Answer the caller's question directly and only from facts.",
        "If facts.followup_guidance contains a relevant entry, use its wording as the basis of the answer.",
        "If the question is not covered by facts, use facts.followup_fallback or offer a human claims representative; never guess.",
        "The caller may switch to one of facts.other_claims; if they do, confirm which claim you are now discussing.",
    ]
    if ctx.just_verified:
        instructions.insert(0, "Identity was just verified this turn: confirm that in a few words, then name the claim and answer.")
    if switched:
        instructions.insert(0, f"The caller switched claims: confirm you are now discussing {claim['case_id']}.")

    directive = Directive(
        phase="PROCESS_CASE",
        goal=f"Help the caller with claim {claim['case_id']} using only grounded facts.",
        facts=facts,
        must_ask="End by asking whether that answers their question or if there is anything else about the claim.",
        instructions=instructions,
        forbidden=["Inventing details not present in facts.", "Discussing claims that belong to other people."],
        fallback_text=FALLBACK_PROCESS_CASE.format(case_id=claim["case_id"]),
    )
    return PhaseResult(decision=f"PROCESS_CASE: answering about {claim['case_id']}", directive=directive)


def _maybe_switch_claim(ctx: TurnContext, claims: list[dict]) -> bool:
    """Switch the active claim when this turn's hints uniquely identify a different one."""
    hints = ctx.extraction.case_hints
    if not hints.any():
        return False
    matches, applied = ctx.store.filter_claims(claims, hints)
    if not applied or len(matches) != 1 or matches[0]["case_id"] == ctx.state.active_case_id:
        return False
    previous, ctx.state.active_case_id = ctx.state.active_case_id, matches[0]["case_id"]
    ctx.decisions.append(f"PROCESS_CASE: switched claim {previous} -> {ctx.state.active_case_id}")
    ctx.progress = True
    return True
