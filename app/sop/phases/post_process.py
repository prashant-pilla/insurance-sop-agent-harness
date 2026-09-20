"""POST_PROCESS: email-summary consent flow, then CLOSED. Also the CLOSED handler."""

from __future__ import annotations

from app.sop.phases import PhaseResult, TurnContext
from app.sop.prompts import COMPANY_NAME, FALLBACK_CLOSED, FALLBACK_POST_PROCESS
from app.sop.responder import draft_email_summary
from app.sop.state import Directive, SessionState
from app.tools.data import normalize_email
from app.tools.email import mask_email, send_email

MAX_EMAIL_OFFERS = 2

# Entry offer wording by remembered preference (Memory.email_preference). The state machine is the same in
# all three cases: an explicit yes on this phase's turn is still required before anything is sent.
ENTRY_OFFER_INSTRUCTIONS: dict[str | None, str] = {
    "yes": (
        "The caller asked earlier for a summary by email; say you remember that request (for example "
        "\"you mentioned earlier that you'd like a summary by email\"), name the masked address, and ask "
        "for a quick yes before it goes out. Nothing has been sent yet: do not say the email is on its way "
        "and do not close the conversation; end with the yes/no question."
    ),
    "no": "The caller said earlier they did not want an email; acknowledge that and ask once whether that still stands.",
    None: "Offer to email a summary of the conversation to the masked address on file.",
}


def handle(ctx: TurnContext) -> PhaseResult:
    state, extraction = ctx.state, ctx.extraction
    holder = ctx.store.get_policyholder(state.party_id or "")
    on_file = holder.get("email") if holder else None
    if not on_file:
        # A record without an email (or a representative record pointing at a missing party) cannot
        # receive a summary; close instead of failing the turn.
        state.phase = "CLOSED"
        return PhaseResult(
            decision="POST_PROCESS: no email on file -> CLOSED without offer",
            directive=_closing(["No email address is on file, so no summary can be sent; close warmly."]),
        )
    masked = mask_email(on_file)

    if ctx.entered_this_turn and extraction.email_consent != "yes":
        state.email_offers += 1
        preference = state.memory.email_preference
        suffix = f" (remembered preference: {preference})" if preference else ""
        return PhaseResult(
            decision=f"POST_PROCESS: offering email summary{suffix}",
            directive=_offer(state, masked, [ENTRY_OFFER_INSTRUCTIONS[preference]]),
        )

    other_address = normalize_email(extraction.email)
    if other_address and other_address != normalize_email(on_file):
        return PhaseResult(
            decision="POST_PROCESS: declined caller-supplied address",
            directive=_offer(
                state,
                masked,
                [
                    "The caller asked to use a different email address. Politely decline: summaries can only go to the address on file.",
                    "Ask whether they would like it sent to the masked on-file address instead.",
                ],
            ),
        )

    if extraction.email_consent == "yes":
        return _send(ctx, on_file, masked)

    if extraction.email_consent == "no":
        state.phase = "CLOSED"
        return PhaseResult(
            decision="POST_PROCESS: email declined -> CLOSED",
            directive=_closing(["The caller declined the email summary; close warmly."]),
        )

    if state.email_offers < MAX_EMAIL_OFFERS:
        state.email_offers += 1
        return PhaseResult(
            decision="POST_PROCESS: consent unclear, re-asking once",
            directive=_offer(
                state,
                masked,
                [
                    "The caller did not clearly answer the email offer. If they asked why consent is needed, explain that claim information is only emailed on request.",
                    "Ask once more, as a yes/no question.",
                ],
            ),
        )

    state.phase = "CLOSED"
    return PhaseResult(
        decision="POST_PROCESS: consent still unclear -> CLOSED without email",
        directive=_closing(["No summary is being sent; mention they can ask for one any time, then close."]),
    )


def handle_closed(ctx: TurnContext) -> PhaseResult:
    extraction = ctx.extraction
    if extraction.scope == "in_scope_claim" and not extraction.conversation_done:
        next_phase = "PROCESS_CASE" if ctx.state.active_case_id else "RESOLVE_INTENT"
        return PhaseResult(decision=f"CLOSED: new in-scope question -> {next_phase}", next_phase=next_phase)
    return PhaseResult(
        decision="CLOSED: closing reply",
        directive=_closing(["The conversation is already closed; reply briefly and warmly without new questions."]),
    )


def _send(ctx: TurnContext, on_file: str, masked: str) -> PhaseResult:
    state = ctx.state
    claim = ctx.store.get_claim(state.active_case_id or "") or {"case_id": "n/a", "case_type": "claim", "status": "unknown"}
    subject, body, note = draft_email_summary(ctx.llm, state, claim, ctx.llm_calls)
    if note:
        ctx.decisions.append(note)
    send_email(state, on_file, subject, body)
    state.phase = "CLOSED"
    return PhaseResult(
        decision=f"POST_PROCESS: email summary sent to {masked} -> CLOSED",
        directive=Directive(
            phase="CLOSED",
            goal="Confirm the summary was sent and close.",
            facts={"sent_to_masked": masked, "email_subject": subject},
            instructions=["Confirm the summary email was sent to the masked address, then close warmly."],
            fallback_text=f"I have sent the summary to {masked}. Thanks for contacting {COMPANY_NAME}, take care.",
        ),
    )


def _offer(state: SessionState, masked: str, instructions: list[str]) -> Directive:
    return Directive(
        phase="POST_PROCESS",
        goal="Offer an emailed summary to the on-file address only.",
        facts={"policyholder_name": state.policyholder_name, "masked_email_on_file": masked, "active_case_id": state.active_case_id},
        must_ask=f"Ask whether they would like the summary emailed to {masked}.",
        instructions=instructions,
        forbidden=["Sending to or acknowledging any address other than the one on file.", "Revealing the unmasked address."],
        fallback_text=FALLBACK_POST_PROCESS.format(masked_email=masked),
    )


def _closing(instructions: list[str]) -> Directive:
    return Directive(
        phase="CLOSED",
        goal="Close the conversation.",
        instructions=instructions,
        fallback_text=FALLBACK_CLOSED.format(company_name=COMPANY_NAME),
    )
