"""Controller: owns the SOP. Runs extraction, memory, phase loop, scope/emotion rules, responder, guard."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from app.config import Settings
from app.llm.base import LLMClient, LLMError, LLMRequestError
from app.sop.extractor import HISTORY_WINDOW as EXTRACTOR_HISTORY_WINDOW
from app.sop.extractor import Extractor
from app.sop.guard import OutputGuard
from app.sop.phases import (
    PhaseResult,
    TurnContext,
    post_process,
    process_case,
    representative,
    resolve_intent,
    verify_id,
)
from app.sop.phases import handoff as handoff_phase
from app.sop.prompts import (
    AGENT_NAME,
    CALLER_CONTEXT_INSTRUCTIONS,
    COMPANY_NAME,
    EMAIL_PREFERENCE_INSTRUCTION,
    FALLBACK_CLOSED,
    FALLBACK_HANDOFF,
    FALLBACK_POST_PROCESS,
    FALLBACK_PROCESS_CASE,
    FALLBACK_REPRESENTATIVE,
    FALLBACK_RESOLVE_INTENT,
    FALLBACK_VERIFY_LOCATE,
    GREETING,
    HANDOFF_QUEUE_REPLY,
    MEMORY_INSTRUCTIONS,
    MODEL_NOT_CONFIGURED_REPLY,
    MODEL_UNAVAILABLE_REPLY,
    OFFER_HUMAN_INSTRUCTION,
    SCOPE_INSTRUCTIONS,
    TONE_GUIDANCE,
)
from app.sop.redact import masked_extraction, redact_pii
from app.sop.responder import HISTORY_WINDOW as RESPONDER_HISTORY_WINDOW
from app.sop.responder import Responder
from app.sop.state import NEGATIVE_EMOTIONS, Directive, Phase, SessionState, TranscriptTurn
from app.sop.trace import Tracer
from app.tools.data import FixtureStore
from app.tools.email import mask_email

logger = logging.getLogger(__name__)

MAX_PHASE_HOPS = 8
# Provider responses that mean the deployment is misconfigured (bad key, wrong model) rather than
# a transient failure; these are surfaced to the tester instead of the phase fallback script.
CONFIG_ERROR_STATUSES = {401, 403, 404}

PhaseHandler = Callable[[TurnContext], PhaseResult]
HANDLERS: dict[str, PhaseHandler] = {
    "VERIFY_ID": verify_id.handle,
    "RESOLVE_INTENT": resolve_intent.handle,
    "PROCESS_CASE": process_case.handle,
    "POST_PROCESS": post_process.handle,
    "CLOSED": post_process.handle_closed,
    "HUMAN_HANDOFF": handoff_phase.handle,
}


@dataclass
class TurnResult:
    reply: str
    state: SessionState
    debug: dict[str, Any] | None


def _empty_debug() -> dict[str, Any]:
    return {
        "extraction": None,
        "decisions": [],
        "directive": None,
        "guard": {"checked": False, "violation": False, "regenerated": False, "fallback_used": False},
        "llm_calls": [],
    }


class Controller:
    def __init__(self, settings: Settings, store: FixtureStore, llm: LLMClient | None, tracer: Tracer):
        self._settings = settings
        self._store = store
        self._llm = llm
        self._tracer = tracer
        self._guard = OutputGuard.default(store)
        self._extractor = Extractor(llm) if llm else None
        self._responder = Responder(llm) if llm else None

    def start_session(self, consent_scenario: str = "default") -> tuple[SessionState, str]:
        valid = self._store.consent_scenarios()
        if consent_scenario not in valid:
            raise ValueError(f"unknown consent_scenario '{consent_scenario}'; valid values: {', '.join(valid)}")
        state = SessionState(session_id=uuid.uuid4().hex[:12], consent_scenario=consent_scenario)
        greeting = GREETING.format(agent_name=AGENT_NAME, company_name=COMPANY_NAME)
        state.transcript.append(TranscriptTurn(role="agent", text=greeting))
        return state, greeting

    def handle_turn(self, state: SessionState, message: str) -> TurnResult:
        state.turn_count += 1
        phase_before = state.phase
        debug = _empty_debug()
        decisions: list[str] = debug["decisions"]

        if state.phase == "HUMAN_HANDOFF":
            decisions.append("HUMAN_HANDOFF: terminal, fixed queue reply")
            return self._finish(state, redact_pii(message, None), HANDOFF_QUEUE_REPLY, phase_before, debug)
        if self._llm is None or self._extractor is None or self._responder is None:
            decisions.append("no LLM configured")
            return self._finish(state, redact_pii(message, None), MODEL_NOT_CONFIGURED_REPLY, phase_before, debug)

        history = state.transcript[-EXTRACTOR_HISTORY_WINDOW:]
        try:
            extraction = self._extractor.extract(message, history, state.slots, state.phase, debug["llm_calls"])
        except LLMError as exc:
            logger.warning("extractor LLM error: %s", exc)
            if isinstance(exc, LLMRequestError) and exc.status_code in CONFIG_ERROR_STATUSES:
                decisions.append(f"extractor failed: HTTP {exc.status_code} (configuration error)")
                reply = MODEL_UNAVAILABLE_REPLY.format(status=exc.status_code)
                return self._finish(state, redact_pii(message, None), reply, phase_before, debug)
            decisions.append("extractor failed: templated fallback, state unchanged")
            return self._finish(state, redact_pii(message, None), self._phase_fallback(state), phase_before, debug)
        debug["extraction"] = masked_extraction(extraction)
        # The extractor has seen the raw message once; the responder, both history windows, the email
        # drafter and the stored transcript only ever get this redacted copy.
        safe = redact_pii(message, extraction)

        ctx = TurnContext(
            state=state,
            extraction=extraction,
            message=safe,
            store=self._store,
            settings=self._settings,
            llm=self._llm,
            llm_calls=debug["llm_calls"],
            decisions=decisions,
        )
        self._update_memory(ctx)
        directive = self._decide(ctx)
        debug["directive"] = directive.model_dump()
        reply = self._render(ctx, directive, state.transcript[-RESPONDER_HISTORY_WINDOW:], debug)
        state.last_emotion = extraction.emotion
        return self._finish(state, safe, reply, phase_before, debug)

    def _update_memory(self, ctx: TurnContext) -> None:
        state, extraction = ctx.state, ctx.extraction
        representative.update_role(ctx)
        if not state.verified and state.caller_role != "representative":
            state.slots.merge(extraction.slots())
        for hint in extraction.intent_hints:
            if hint and hint not in state.memory.intent_hints:
                state.memory.intent_hints.append(hint)
        state.memory.case_hints.merge(extraction.case_hints)
        if extraction.intent_path:
            state.intent_path = extraction.intent_path
        if extraction.wants_human:
            state.memory.notes.append("caller asked for a human")
        # An early "email me a summary" / "no email please" is remembered so POST_PROCESS can confirm it
        # instead of asking cold. Replies inside POST_PROCESS are consent for that offer, not a preference.
        if state.phase != "POST_PROCESS" and extraction.email_consent != "unclear":
            preference_notes = {
                "yes": "caller asked for an emailed summary",
                "no": "caller declined an emailed summary",
            }
            preference = extraction.email_consent
            state.memory.email_preference = preference
            stale = preference_notes["no" if preference == "yes" else "yes"]
            if stale in state.memory.notes:
                state.memory.notes.remove(stale)
            if preference_notes[preference] not in state.memory.notes:
                state.memory.notes.append(preference_notes[preference])

    def _decide(self, ctx: TurnContext) -> Directive:
        state, extraction, settings = ctx.state, ctx.extraction, self._settings
        if extraction.wants_human:
            return self._handoff(ctx, "caller requested a human representative")

        if extraction.scope == "out_of_scope":
            state.off_topic_streak += 1
            ctx.decisions.append(f"scope: out_of_scope (streak {state.off_topic_streak})")
            if state.off_topic_streak >= settings.max_off_topic:
                return self._handoff(ctx, f"{state.off_topic_streak} consecutive off-topic messages")
        elif extraction.scope in ("in_scope_claim", "in_scope_general"):
            state.off_topic_streak = 0

        directive = self._run_phases(ctx)
        if state.phase == "HUMAN_HANDOFF":
            # A handler transferred the caller itself; skip scope/memory decoration like _handoff does.
            directive.tone = TONE_GUIDANCE.get(extraction.emotion, TONE_GUIDANCE["neutral"])
            return directive

        if extraction.emotion in NEGATIVE_EMOTIONS and not ctx.progress:
            state.frustration_streak += 1
            ctx.decisions.append(f"emotion: {extraction.emotion} without progress (streak {state.frustration_streak})")
            if state.frustration_streak >= settings.max_frustration_turns:
                return self._handoff(ctx, f"{state.frustration_streak} frustrated turns without progress")
        else:
            state.frustration_streak = 0

        self._apply_scope(ctx, directive)
        self._attach_memory(ctx, directive)
        self._attach_caller(ctx, directive)
        directive.tone = TONE_GUIDANCE.get(extraction.emotion, TONE_GUIDANCE["neutral"])
        return directive

    def _attach_caller(self, ctx: TurnContext, directive: Directive) -> None:
        """After consent, tell the responder it is speaking to the representative, not the policyholder."""
        state = ctx.state
        rep = state.representative
        if state.caller_role != "representative" or rep is None or directive.phase == "VERIFY_ID":
            return
        directive.facts["caller"] = {
            "role": "representative",
            "representative_name": rep.name,
            "relationship": rep.relationship,
            "on_behalf_of": rep.on_behalf_of,
            "consent_status": state.consent_status,
        }
        if ctx.just_verified:
            # Approval and the first answer happen in the same turn; the responder must not keep "waiting".
            directive.instructions.insert(0, CALLER_CONTEXT_INSTRUCTIONS["just_approved"])
        directive.instructions.append(CALLER_CONTEXT_INSTRUCTIONS["default"])
        if directive.phase == "POST_PROCESS":
            directive.instructions.append(CALLER_CONTEXT_INSTRUCTIONS["POST_PROCESS"])

    def _attach_memory(self, ctx: TurnContext, directive: Directive) -> None:
        """Expose what the caller has already told us so the responder never asks twice."""
        memory = ctx.state.memory
        if not memory.intent_hints and not memory.notes:
            return
        directive.facts["caller_context"] = {
            "stated_reasons_for_calling": list(memory.intent_hints),
            "notes": list(memory.notes),
        }
        key = "VERIFY_ID" if directive.phase == "VERIFY_ID" else "default"
        directive.instructions.append(MEMORY_INSTRUCTIONS[key])
        # POST_PROCESS handles the remembered request itself (ENTRY_OFFER_INSTRUCTIONS); before that,
        # the responder must acknowledge it without promising a send or a follow-up by someone else.
        if memory.email_preference == "yes" and directive.phase in ("VERIFY_ID", "RESOLVE_INTENT", "PROCESS_CASE"):
            directive.instructions.append(EMAIL_PREFERENCE_INSTRUCTION)

    def _run_phases(self, ctx: TurnContext) -> Directive:
        state = ctx.state
        for _ in range(MAX_PHASE_HOPS):
            result = HANDLERS[state.phase](ctx)
            ctx.decisions.append(result.decision)
            if result.directive is not None:
                return result.directive
            assert result.next_phase is not None
            state.phase = result.next_phase
            ctx.entered_this_turn = True
            ctx.progress = True
        raise RuntimeError("phase loop did not converge")

    def _apply_scope(self, ctx: TurnContext, directive: Directive) -> None:
        scope = ctx.extraction.scope
        instruction = SCOPE_INSTRUCTIONS.get(scope)
        if instruction:
            directive.instructions.insert(0, instruction.format(company_name=COMPANY_NAME))
            directive.facts["scope"] = scope
        if scope == "out_of_scope" and ctx.state.off_topic_streak >= 2:
            directive.instructions.insert(1, OFFER_HUMAN_INSTRUCTION)
        elif scope == "in_scope_general" and OFFER_HUMAN_INSTRUCTION not in directive.instructions:
            directive.instructions.append(OFFER_HUMAN_INSTRUCTION)

    def _handoff(self, ctx: TurnContext, reason: str) -> Directive:
        ctx.handoff_reason = reason
        result = handoff_phase.handle(ctx)
        ctx.decisions.append(result.decision)
        assert result.directive is not None
        result.directive.tone = TONE_GUIDANCE.get(ctx.extraction.emotion, TONE_GUIDANCE["neutral"])
        return result.directive

    def _render(
        self, ctx: TurnContext, directive: Directive, history: list[TranscriptTurn], debug: dict[str, Any]
    ) -> str:
        assert self._responder is not None
        guard: dict[str, bool] = debug["guard"]
        try:
            reply = self._responder.respond(directive, history, ctx.message, ctx.llm_calls)
        except LLMError as exc:
            logger.warning("responder LLM error: %s", type(exc).__name__)
            ctx.decisions.append("responder failed: templated fallback")
            return directive.fallback_text
        state = ctx.state
        checked = self._guard.applicable(state, directive)
        guard["checked"] = checked
        violations = self._guard.check(reply, state, directive) if checked else []
        if not violations:
            return reply
        guard["violation"] = True
        guard["regenerated"] = True
        # Rule name and hit count only: for claim_leak the hits are the very strings the guard keeps out.
        for violation in violations:
            ctx.decisions.append(f"guard[{violation.rule}]: blocked {len(violation.hits)} hit(s), regenerating")
        try:
            reply = self._responder.respond(
                directive, history, ctx.message, ctx.llm_calls, corrections=[v.correction for v in violations]
            )
        except LLMError:
            reply = ""
        if not reply or self._guard.check(reply, state, directive):
            guard["fallback_used"] = True
            ctx.decisions.append("guard: regenerated reply still unsafe, templated fallback used")
            return directive.fallback_text
        return reply

    def _phase_fallback(self, state: SessionState) -> str:
        if state.phase == "VERIFY_ID":
            rep = state.representative
            if state.caller_role == "representative" and rep is not None:
                if rep.party_id is not None:
                    return FALLBACK_REPRESENTATIVE["pending"].format(
                        representative_name=rep.name, on_behalf_of=rep.on_behalf_of
                    )
                return FALLBACK_REPRESENTATIVE["identify"].format(company_name=COMPANY_NAME)
            return FALLBACK_VERIFY_LOCATE.format(company_name=COMPANY_NAME)
        if state.phase == "RESOLVE_INTENT":
            return FALLBACK_RESOLVE_INTENT
        if state.phase == "PROCESS_CASE":
            return FALLBACK_PROCESS_CASE.format(case_id=state.active_case_id or "on file")
        if state.phase == "POST_PROCESS":
            holder = self._store.get_policyholder(state.party_id or "")
            email = holder.get("email") if holder else None
            masked = mask_email(email) if email else "your address on file"
            return FALLBACK_POST_PROCESS.format(masked_email=masked)
        if state.phase == "HUMAN_HANDOFF":
            return FALLBACK_HANDOFF
        return FALLBACK_CLOSED.format(company_name=COMPANY_NAME)

    def _finish(
        self, state: SessionState, message: str, reply: str, phase_before: Phase, debug: dict[str, Any]
    ) -> TurnResult:
        state.transcript.append(TranscriptTurn(role="user", text=message))
        state.transcript.append(TranscriptTurn(role="agent", text=reply))
        self._tracer.record(state.session_id, state.turn_count, phase_before, state.phase, debug)
        logger.info("session=%s turn=%d %s -> %s", state.session_id, state.turn_count, phase_before, state.phase)
        state.last_debug = debug if self._tracer.enabled else None
        return TurnResult(reply=reply, state=state, debug=state.last_debug)
