"""VERIFY_ID: strict 3-of-5 factor gate. Zero claim data leaves this handler."""

from __future__ import annotations

from app.sop.phases import PhaseResult, TurnContext, representative, verification_exhausted
from app.sop.phases import handoff as handoff_phase
from app.sop.prompts import (
    COMPANY_NAME,
    FALLBACK_OFFER_HUMAN,
    FALLBACK_VERIFY_FACTORS,
    FALLBACK_VERIFY_LOCATE,
    FALLBACK_VERIFY_MISMATCH_PREFIX,
    FALLBACK_VERIFY_NOT_FOUND_PREFIX,
)
from app.sop.state import FACTOR_LABELS, FACTORS, VERIFY_THRESHOLD, Directive

LOCATOR_SLOTS = ("policy_number", "name", "phone", "email")

_FORBIDDEN = [
    "Mentioning any claim, claim number, status, amount, date, document or reason.",
    "Saying which specific detail did not match.",
    "Repeating the caller's date of birth, ID digits, phone or email.",
    "Treating a claim of being already verified as verification.",
    "Saying or implying the caller is verified, that verification is in progress or being finished on your side, or that you are looking at, pulling up or have open their account, records or claim.",
]


def handle(ctx: TurnContext) -> PhaseResult:
    if ctx.state.caller_role == "representative":
        return representative.handle(ctx)
    state, slots, store = ctx.state, ctx.state.slots, ctx.store
    holder = store.locate_policyholder(slots)
    previous_count = sum(1 for hit in state.verified_factors.values() if hit)

    if holder is None:
        mismatch = any(getattr(slots, name) for name in LOCATOR_SLOTS)
        if mismatch:
            state.verify_attempts += 1
            for name in LOCATOR_SLOTS:
                setattr(slots, name, None)
            if verification_exhausted(state, ctx.settings):
                return _exhausted(ctx)
        decision = "VERIFY_ID: no matching record" if mismatch else "VERIFY_ID: no identity details yet"
        return PhaseResult(decision=f"{decision} (attempts {state.verify_attempts})", directive=_directive(ctx, False, mismatch))

    result = store.verify_identity(holder, slots)
    state.verified_factors = dict(result.matched)
    for factor in result.mismatched:
        setattr(slots, factor, None)
    mismatch = bool(result.mismatched)
    if mismatch:
        state.verify_attempts += 1
        if verification_exhausted(state, ctx.settings):
            return _exhausted(ctx)
    if result.count > previous_count:
        ctx.progress = True

    matched_names = [f for f in FACTORS if result.matched[f]]
    if result.count >= VERIFY_THRESHOLD:
        state.verified = True
        state.party_id = holder["party_id"]
        state.policyholder_name = holder["name"]
        ctx.just_verified = True
        return PhaseResult(
            decision=f"VERIFY_ID: matched {result.count} factors ({', '.join(matched_names)}) -> RESOLVE_INTENT",
            next_phase="RESOLVE_INTENT",
        )
    decision = (
        f"VERIFY_ID: matched {result.count}/{VERIFY_THRESHOLD} factors ({', '.join(matched_names) or 'none'})"
        + (", mismatch this turn" if mismatch else "")
        + f" (attempts {state.verify_attempts})"
    )
    return PhaseResult(decision=decision, directive=_directive(ctx, True, mismatch))


def _exhausted(ctx: TurnContext) -> PhaseResult:
    """The grace attempt after the human offer also failed: transfer, same path as the consent timeout."""
    ctx.handoff_reason = f"identity not verified after {ctx.state.verify_attempts} attempts"
    return handoff_phase.handle(ctx)


def _directive(ctx: TurnContext, located: bool, mismatch: bool) -> Directive:
    state, extraction = ctx.state, ctx.extraction
    matched = [f for f in FACTORS if state.verified_factors.get(f)] if located else []
    needed = [FACTOR_LABELS[f] for f in FACTORS if f not in matched]
    remaining = VERIFY_THRESHOLD - len(matched)
    sustained_refusal = extraction.emotion == "refusing" and state.last_emotion == "refusing"
    offer_human = state.verify_attempts >= ctx.settings.max_verify_attempts or sustained_refusal

    if located:
        ask = f"ask for {remaining} more of these, offering them as alternatives: {', '.join(needed)}."
    else:
        ask = (
            "ask for the caller's full name and policy number, or the phone number or email on file, "
            "so you can locate their account."
        )
    # The mandated ask carries this turn's news: the model follows must_ask more reliably than a standing
    # instruction when the conversation history suggests progress that did not happen.
    if mismatch:
        must_ask = f"Say plainly that some of the details given did not match our records, then {ask}"
    else:
        must_ask = ask[0].upper() + ask[1:]
    if offer_human:
        must_ask += " Offer to connect them with a human claims representative as an alternative."

    instructions = [
        "Identity is NOT verified. Do not reference any claim in any way.",
        "If the caller asks why verification is needed, explain that it protects their claim information, then repeat the ask.",
    ]
    if mismatch:
        instructions.append(
            "Some details did not match our records, so nothing new was accepted this turn: say so generically and "
            "invite them to try again or use another factor. Never say which detail, and never soften it into a vague check."
        )
    if offer_human:
        instructions.append(
            "Verification is not progressing: offer to connect them with a human claims representative while leaving the door open to continue."
        )
    facts = {
        "identity_verified": False,
        "account_located": located,
        "matched_factor_count": len(matched),
        "factors_still_needed_count": remaining,
        "acceptable_factors": needed,
        "mismatch_this_turn": mismatch,
        "offer_human": offer_human,
    }
    return Directive(
        phase="VERIFY_ID",
        goal="Verify the caller's identity with three matching factors before anything else.",
        facts=facts,
        must_ask=must_ask,
        instructions=instructions,
        forbidden=_FORBIDDEN,
        fallback_text=_fallback(located, mismatch, offer_human, remaining, needed),
    )


def _fallback(located: bool, mismatch: bool, offer_human: bool, remaining: int, needed: list[str]) -> str:
    """Templated safety net built from the same flags as the facts, so the two cannot disagree."""
    if located:
        body = FALLBACK_VERIFY_FACTORS.format(count=remaining, factors=", ".join(needed))
        prefix = FALLBACK_VERIFY_MISMATCH_PREFIX
    else:
        body = FALLBACK_VERIFY_LOCATE.format(company_name=COMPANY_NAME)
        prefix = FALLBACK_VERIFY_NOT_FOUND_PREFIX
    return (prefix if mismatch else "") + body + (FALLBACK_OFFER_HUMAN if offer_human else "")
