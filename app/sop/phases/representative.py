"""VERIFY_ID/REP: sub-flow for a caller acting on behalf of the policyholder.

The phase stays VERIFY_ID throughout. Instead of the 3-factor gate, the representative is matched
against representatives.json on two names and then waits for the policyholder's (simulated) consent.
A representative supplying the policyholder's factors never verifies; only consent does.
"""

from __future__ import annotations

from typing import Any

from app.sop.phases import PhaseResult, TurnContext, verification_exhausted
from app.sop.phases import handoff as handoff_phase
from app.sop.prompts import COMPANY_NAME, FALLBACK_REPRESENTATIVE, REPRESENTATIVE_INSTRUCTIONS
from app.sop.state import FACTORS, Directive, Extraction, IdentitySlots, RepresentativeInfo, SessionState
from app.tools.data import normalize_name

DECISION_PREFIX = "VERIFY_ID/REP"
CONSENT_TIMEOUT_REASON = "policyholder consent not received"

_FORBIDDEN = [
    "Mentioning any claim, claim number, status, amount, date, document or reason.",
    "Confirming or denying whether the named person is a customer or has a policy with us.",
    "Asking for the policyholder's date of birth, ID digits, phone number or email address.",
    "Treating identity details supplied by the caller as verification of the policyholder.",
    "Repeating any date of birth, ID digits, phone or email the caller may have mentioned.",
]


def update_role(ctx: TurnContext) -> None:
    """Role bookkeeping, run before the PII slot merge.

    Enter: extractor says caller_role == representative AND names a relationship or a person they call
    for (two signals). Reset: an explicit caller_role == policyholder AND a stated name.
    While in the sub-flow and unmatched, newly stated representative details are merged.
    """
    state, extraction = ctx.state, ctx.extraction
    if state.verified:
        return

    if state.caller_role != "representative":
        two_signals = extraction.caller_role == "representative" and bool(
            extraction.on_behalf_of_name or extraction.relationship
        )
        if not two_signals or state.phase != "VERIFY_ID":
            return
        state.caller_role = "representative"
        state.representative = RepresentativeInfo(name=_stated_rep_name(extraction) or "")
        state.consent_status = None
        state.consent_checks = 0
        state.slots = IdentitySlots()
        state.verified_factors = {f: False for f in FACTORS}
        ctx.decisions.append(f"{DECISION_PREFIX}: caller identified as a representative")
    elif extraction.caller_role == "policyholder" and extraction.name:
        state.caller_role = "policyholder"
        state.representative = None
        state.consent_status = None
        state.consent_checks = 0
        ctx.decisions.append(f"{DECISION_PREFIX}: caller now states they are the policyholder, role reset")
        return

    rep = state.representative
    if rep is None or rep.party_id is not None:
        return
    stated_name = _stated_rep_name(extraction)
    if stated_name:
        rep.name = stated_name
    if extraction.relationship:
        rep.relationship = extraction.relationship
    if extraction.on_behalf_of_name:
        rep.on_behalf_of = extraction.on_behalf_of_name


def handle(ctx: TurnContext) -> PhaseResult:
    state, store = ctx.state, ctx.store
    rep = state.representative
    assert rep is not None

    if rep.party_id is None:
        missing = [
            label
            for label, value in (("your full name", rep.name), ("the policyholder's full name", rep.on_behalf_of))
            if not value
        ]
        if missing:
            return PhaseResult(
                decision=f"{DECISION_PREFIX}: awaiting representative details (missing: {', '.join(missing)})",
                directive=_identify_directive(ctx, missing, mismatch=False),
            )
        record = store.find_representative(rep.name, rep.on_behalf_of)
        if record is None:
            extraction = ctx.extraction
            if extraction.representative_name or extraction.on_behalf_of_name or extraction.name:
                state.verify_attempts += 1  # only turns that offered names count as attempts
                if verification_exhausted(state, ctx.settings):
                    ctx.handoff_reason = (
                        f"representative authorization not found after {state.verify_attempts} attempts"
                    )
                    return handoff_phase.handle(ctx)
            return PhaseResult(
                decision=(
                    f"{DECISION_PREFIX}: no authorization on file for {rep.name} / {rep.on_behalf_of} "
                    f"(attempts {state.verify_attempts})"
                ),
                directive=_identify_directive(ctx, [], mismatch=True),
            )
        holder = store.get_policyholder(record.get("buyer_party_id", ""))
        rep.party_id = record.get("buyer_party_id")
        rep.on_behalf_of = holder["name"] if holder else record.get("buyer_name", rep.on_behalf_of)
        rep.relationship = rep.relationship or record.get("relationship")
        state.consent_checks = 1
        ctx.progress = True
        matched = f"matched {rep.name} for {rep.on_behalf_of}, consent requested"
    else:
        state.consent_checks += 1
        matched = f"consent check for {rep.on_behalf_of} via {rep.name}"

    status = store.consent_status(state.consent_scenario, state.consent_checks)
    state.consent_status = status  # type: ignore[assignment]

    if status == "approved":
        state.verified = True
        state.party_id = rep.party_id
        state.policyholder_name = rep.on_behalf_of
        ctx.just_verified = True
        return PhaseResult(
            decision=f"{DECISION_PREFIX}: {matched} (approved, check {state.consent_checks}) -> RESOLVE_INTENT",
            next_phase="RESOLVE_INTENT",
        )
    if status == "timed_out":
        ctx.decisions.append(f"{DECISION_PREFIX}: {matched} (timed_out, check {state.consent_checks}) -> HUMAN_HANDOFF")
        ctx.handoff_reason = CONSENT_TIMEOUT_REASON
        return handoff_phase.handle(ctx)
    return PhaseResult(
        decision=f"{DECISION_PREFIX}: {matched} (pending, check {state.consent_checks})",
        directive=_pending_directive(ctx),
    )


def _stated_rep_name(extraction: Extraction) -> str | None:
    """The caller's own name; falls back to ``name`` when the extractor put it there by mistake."""
    if extraction.representative_name:
        return extraction.representative_name
    if extraction.name and normalize_name(extraction.name) != normalize_name(extraction.on_behalf_of_name):
        return extraction.name
    return None


def _facts(state: SessionState, offer_human: bool) -> dict[str, Any]:
    rep = state.representative
    assert rep is not None
    return {
        "representative_name": rep.name or None,
        "relationship": rep.relationship,
        "on_behalf_of": rep.on_behalf_of,
        "authorization_found": rep.party_id is not None,
        "consent_status": state.consent_status,
        "consent_checks": state.consent_checks,
        "offer_human": offer_human,
    }


def _offer_human(ctx: TurnContext) -> bool:
    state, extraction = ctx.state, ctx.extraction
    sustained_refusal = extraction.emotion == "refusing" and state.last_emotion == "refusing"
    return state.verify_attempts >= ctx.settings.max_verify_attempts or sustained_refusal


def _identify_directive(ctx: TurnContext, missing: list[str], mismatch: bool) -> Directive:
    offer_human = _offer_human(ctx)
    instructions = [
        "Identity is NOT verified. Do not reference any claim or account in any way.",
        REPRESENTATIVE_INSTRUCTIONS["identify"],
    ]
    if mismatch:
        instructions.append(REPRESENTATIVE_INSTRUCTIONS["mismatch"])
        must_ask = "Ask them to confirm their own full name and the full name of the policyholder they are calling for."
        fallback = FALLBACK_REPRESENTATIVE["mismatch"]
    else:
        must_ask = f"Ask for {' and '.join(missing)} so you can check for an authorization on file."
        fallback = FALLBACK_REPRESENTATIVE["identify"].format(company_name=COMPANY_NAME)
    if offer_human:
        instructions.append(REPRESENTATIVE_INSTRUCTIONS["offer_human"])
    return Directive(
        phase="VERIFY_ID",
        goal="Identify the representative and the policyholder they are calling for, then check for an authorization on file.",
        facts=_facts(ctx.state, offer_human),
        must_ask=must_ask,
        instructions=instructions,
        forbidden=_FORBIDDEN,
        fallback_text=fallback,
    )


def _pending_directive(ctx: TurnContext) -> Directive:
    state = ctx.state
    rep = state.representative
    assert rep is not None
    return Directive(
        phase="VERIFY_ID",
        goal="Wait for the policyholder's consent before discussing anything about the account.",
        facts=_facts(state, False),
        must_ask="Invite them to continue when ready or to let you know if there is anything to note while the request is pending.",
        instructions=[
            "Identity is NOT verified. Do not reference any claim or account detail in any way.",
            REPRESENTATIVE_INSTRUCTIONS["pending"],
        ],
        forbidden=_FORBIDDEN,
        fallback_text=FALLBACK_REPRESENTATIVE["pending"].format(
            representative_name=rep.name, on_behalf_of=rep.on_behalf_of
        ),
    )
