"""Representative consent sub-flow (VERIFY_ID/REP): extraction model, role rules, handler, controller."""

from __future__ import annotations

import json
from typing import Any, Callable

import pytest

from app.llm.base import LLMError, LLMRequestError
from app.sop.phases.representative import CONSENT_TIMEOUT_REASON, DECISION_PREFIX
from app.sop.prompts import CALLER_CONTEXT_INSTRUCTIONS, HANDOFF_QUEUE_REPLY, OFFER_HUMAN_INSTRUCTION
from app.sop.state import FACTORS, Extraction
from tests.conftest import DEMO_EXTRACTION, DEMO_UTTERANCE, FakeLLM, Harness

REP_UTTERANCE = (
    "Hi, this is David Chen. I'm calling on behalf of my mother, Margaret Chen, about her denied "
    "healthcare claim from January."
)
REP_EXTRACTION: dict[str, Any] = {
    "caller_role": "representative",
    "representative_name": "David Chen",
    "relationship": "son",
    "on_behalf_of_name": "Margaret Chen",
    "intent_hints": ["denied healthcare claim from January"],
    "intent_path": "denial_question",
    "case_hints": {"case_type": "healthcare", "status": "denied", "time_hint": "January"},
    "scope": "in_scope_claim",
    "emotion": "neutral",
}
WAITING = "Okay, has she approved yet?"
NO_FACTORS = {f: False for f in FACTORS}


def _rep_harness(make_harness: Callable[..., Harness], scenario: str = "default", **kwargs: Any) -> Harness:
    """Harness whose scripts already know the representative preset; scenario picks the consent sequence."""
    scripts: dict[str, dict[str, Any]] = {"on behalf of my mother": REP_EXTRACTION}
    scripts.update(kwargs.pop("scripts", {}))
    harness = make_harness(scripts, **kwargs)
    harness.state, harness.greeting = harness.controller.start_session(consent_scenario=scenario)
    return harness


def _match(harness: Harness):
    """Play the representative preset and assert the record matched (consent check 1)."""
    result = harness.say(REP_UTTERANCE)
    assert harness.state.caller_role == "representative"
    assert harness.state.consent_status == "pending"
    assert harness.state.consent_checks == 1
    return result


def _directive(result) -> dict[str, Any]:
    assert result.debug is not None and result.debug["directive"] is not None
    return result.debug["directive"]


# --------------------------------------------------------------------------------------------------
# Extraction model
# --------------------------------------------------------------------------------------------------


def test_extraction_normalizes_role_and_blank_representative_fields():
    extraction = Extraction.model_validate({"caller_role": "REPRESENTATIVE ", "representative_name": " "})
    assert extraction.caller_role == "representative"
    assert extraction.representative_name is None
    assert extraction.relationship is None and extraction.on_behalf_of_name is None


def test_extraction_unknown_role_becomes_none_without_dropping_other_fields():
    extraction = Extraction.model_validate({"caller_role": "agent", "name": "x"})
    assert extraction.caller_role is None
    assert extraction.name == "x"


def test_extraction_legacy_scripts_still_validate():
    extraction = Extraction.model_validate(DEMO_EXTRACTION)
    assert extraction.name == "Margaret Chen"
    assert extraction.caller_role is None
    assert Extraction.model_validate({}).caller_role is None


# --------------------------------------------------------------------------------------------------
# Role rules (update_role)
# --------------------------------------------------------------------------------------------------


def test_role_entered_with_on_behalf_of_name(make_harness):
    harness: Harness = make_harness(
        {"for margaret": {"caller_role": "representative", "on_behalf_of_name": "Margaret Chen"}}
    )
    result = harness.say("I'm calling for Margaret Chen")
    assert harness.state.caller_role == "representative"
    assert harness.state.representative is not None
    assert result.debug is not None
    assert f"{DECISION_PREFIX}: caller identified as a representative" in result.debug["decisions"]


def test_role_entered_with_relationship_only(make_harness):
    harness: Harness = make_harness({"my mother": {"caller_role": "representative", "relationship": "son"}})
    harness.say("I'm calling about my mother's account")
    assert harness.state.caller_role == "representative"


def test_role_not_entered_on_single_signal(make_harness):
    harness: Harness = make_harness({"representative": {"caller_role": "representative"}})
    result = harness.say("I'm a representative")
    assert harness.state.caller_role == "policyholder"
    assert harness.state.representative is None
    assert "acceptable_factors" in _directive(result)["facts"]
    assert "caller_role" not in harness.state.public_state()


def test_first_representative_turn_does_not_merge_pii_slots(make_harness):
    harness: Harness = make_harness(
        {
            "on behalf": {
                **REP_EXTRACTION,
                "policy_number": "POL-9921",
                "dob": "1985-03-15",
            }
        }
    )
    harness.say("Hi, David Chen on behalf of my mother Margaret Chen, policy POL-9921, DOB 1985-03-15")
    assert harness.state.caller_role == "representative"
    assert harness.state.slots.provided() == []
    assert harness.state.verified_factors == NO_FACTORS


def test_role_reset_requires_a_name(make_harness):
    harness: Harness = _rep_harness(make_harness, "timeout", scripts={"has she approved": {"caller_role": "policyholder"}})
    _match(harness)

    # A bare caller_role=policyholder (the pronoun refers to the policyholder) must not flip the role.
    result = harness.say("Has she approved yet?")
    assert harness.state.caller_role == "representative"
    assert harness.state.representative is not None and harness.state.representative.party_id == "P9"
    assert harness.state.consent_status == "pending" and harness.state.consent_checks == 2
    assert not any("role reset" in d for d in result.debug["decisions"])


def test_role_reset_with_name_clears_representative_state_and_verifies_normally(make_harness):
    harness: Harness = make_harness(
        {
            "on behalf": {
                "caller_role": "representative",
                "representative_name": "Bob Jones",
                "relationship": "nephew",
                "on_behalf_of_name": "Margaret Chen",
            },
            "no, i am margaret": {"caller_role": "policyholder", "name": "Margaret Chen"},
            "last four": {"dob": "1985-03-15", "id_last4": "4472"},
        }
    )
    harness.say("Hi, Bob Jones on behalf of Margaret Chen")
    assert harness.state.caller_role == "representative"
    assert harness.state.verify_attempts == 1

    reset = harness.say("No, I am Margaret Chen, sorry for the confusion")
    assert harness.state.caller_role == "policyholder"
    assert harness.state.representative is None
    assert harness.state.consent_status is None and harness.state.consent_checks == 0
    assert reset.debug is not None
    assert any("role reset" in d for d in reset.debug["decisions"])
    assert harness.state.slots.name == "Margaret Chen"
    assert harness.state.verified_factors["name"] is True
    assert "caller_role" not in harness.state.public_state()

    harness.say("DOB 1985-03-15 and last four 4472")
    assert harness.state.verified is True
    assert harness.state.policyholder_name == "Margaret Chen"
    assert harness.state.verified_factors == {"name": True, "dob": True, "phone": False, "email": False, "id_last4": True}
    assert harness.state.phase == "RESOLVE_INTENT"


def test_demo_caller_unaffected_by_representative_hooks(make_harness):
    harness: Harness = make_harness(
        {"margaret chen, policy pol-9921": {**DEMO_EXTRACTION, "caller_role": "policyholder"}}
    )
    result = harness.say(DEMO_UTTERANCE)
    assert harness.state.phase == "PROCESS_CASE"
    assert harness.state.active_case_id == "CL-2048"
    assert harness.state.caller_role == "policyholder"
    public = harness.state.public_state()
    for key in ("caller_role", "representative_name", "representative_relationship", "consent_status"):
        assert key not in public
    assert "caller" not in _directive(result)["facts"]
    assert not any(d.startswith(DECISION_PREFIX) for d in result.debug["decisions"])


# --------------------------------------------------------------------------------------------------
# Representative handler: identification
# --------------------------------------------------------------------------------------------------


def test_rep_name_only_asks_for_policyholder(make_harness):
    harness: Harness = make_harness(
        {"my mom": {"caller_role": "representative", "representative_name": "David Chen", "relationship": "son"}}
    )
    result = harness.say("Hi, David Chen here, calling about my mom's claim")
    directive = _directive(result)
    assert harness.state.phase == "VERIFY_ID"
    assert "policyholder's full name" in directive["must_ask"]
    assert "your full name" not in directive["must_ask"]
    assert "acceptable_factors" not in directive["facts"]
    assert directive["facts"]["authorization_found"] is False
    lowered = directive["must_ask"].lower()
    for factor in ("date of birth", "phone", "email", "ssn", "last 4"):
        assert factor not in lowered
    assert any("awaiting representative details" in d for d in result.debug["decisions"])


def test_policyholder_only_asks_for_rep_name(make_harness):
    harness: Harness = make_harness(
        {"for margaret": {"caller_role": "representative", "on_behalf_of_name": "Margaret Chen"}}
    )
    result = harness.say("I'm calling for Margaret Chen")
    directive = _directive(result)
    assert "your full name" in directive["must_ask"]
    assert "policyholder's full name" not in directive["must_ask"]
    assert "acceptable_factors" not in directive["facts"]
    assert directive["facts"]["representative_name"] is None
    assert directive["facts"]["on_behalf_of"] == "Margaret Chen"


def test_unknown_pair_is_generic_mismatch_and_third_offers_human(make_harness):
    harness: Harness = make_harness(
        {
            "bob jones": {
                "caller_role": "representative",
                "representative_name": "Bob Jones",
                "relationship": "nephew",
                "on_behalf_of_name": "Margaret Chen",
            }
        }
    )
    result = harness.say("Hi, Bob Jones calling for my aunt Margaret Chen")
    assert harness.state.verify_attempts == 1
    assert harness.state.consent_status is None
    directive = _directive(result)
    assert directive["facts"]["authorization_found"] is False
    assert directive["facts"]["offer_human"] is False
    assert any("could not find an authorization on file" in i for i in directive["instructions"])
    assert any("is a customer" in f for f in directive["forbidden"])
    assert any(d.startswith(f"{DECISION_PREFIX}: no authorization on file for Bob Jones / Margaret Chen") for d in result.debug["decisions"])
    assert "CL-" not in result.reply

    harness.say("It's Bob Jones, for Margaret Chen")
    assert harness.state.verify_attempts == 2
    third = harness.say("Bob Jones, Margaret Chen, I told you")
    assert harness.state.verify_attempts == 3
    assert _directive(third)["facts"]["offer_human"] is True
    assert harness.state.phase == "VERIFY_ID"


def test_mismatch_turn_without_names_does_not_count_as_attempt(make_harness):
    harness: Harness = make_harness(
        {
            "bob jones": {
                "caller_role": "representative",
                "representative_name": "Bob Jones",
                "on_behalf_of_name": "Margaret Chen",
            },
            "why": {},
        }
    )
    harness.say("Bob Jones calling for Margaret Chen")
    assert harness.state.verify_attempts == 1
    harness.say("Why can't you find it?")
    assert harness.state.verify_attempts == 1


def test_rep_name_falls_back_to_name_when_extractor_misplaces_it(make_harness):
    harness: Harness = make_harness(
        {
            "on behalf": {
                "caller_role": "representative",
                "name": "David Chen",
                "relationship": "son",
                "on_behalf_of_name": "Margaret Chen",
            }
        }
    )
    harness.say("David Chen on behalf of my mother Margaret Chen")
    assert harness.state.representative is not None
    assert harness.state.representative.name == "David Chen"
    assert harness.state.consent_status == "pending"
    assert harness.state.slots.name is None


# --------------------------------------------------------------------------------------------------
# Representative handler: match and consent
# --------------------------------------------------------------------------------------------------


def test_match_requests_consent_without_verifying(make_harness):
    harness: Harness = _rep_harness(make_harness)
    result = _match(harness)
    state = harness.state
    assert state.phase == "VERIFY_ID"
    assert state.verified is False
    assert state.party_id is None
    assert state.policyholder_name is None
    assert state.representative is not None
    assert state.representative.party_id == "P9"
    assert state.representative.relationship == "son"
    assert state.representative.on_behalf_of == "Margaret Chen"
    decisions = result.debug["decisions"]
    assert f"{DECISION_PREFIX}: matched David Chen for Margaret Chen, consent requested (pending, check 1)" in decisions
    directive = _directive(result)
    assert directive["facts"]["authorization_found"] is True
    assert directive["facts"]["consent_status"] == "pending"
    assert "acceptable_factors" not in directive["facts"]
    assert "identity" not in directive["must_ask"].lower()
    assert result.debug["guard"]["checked"] is True
    assert "CL-" not in result.reply
    public = state.public_state()
    assert public["caller_role"] == "representative"
    assert public["representative_name"] == "David Chen"
    assert public["representative_relationship"] == "son"
    assert public["consent_status"] == "pending"
    assert public["policyholder_name"] is None
    assert state.memory.case_hints.status == "denied"


def test_default_scenario_approves_on_next_turn_and_resolves_claim(make_harness):
    harness: Harness = _rep_harness(make_harness)
    _match(harness)
    result = harness.say(WAITING)
    state = harness.state
    assert state.consent_status == "approved"
    assert state.consent_checks == 2
    assert state.verified is True
    assert state.party_id == "P9"
    assert state.policyholder_name == "Margaret Chen"
    assert state.verified_factors == NO_FACTORS
    assert state.phase == "PROCESS_CASE"
    assert state.active_case_id == "CL-2048"
    decisions = result.debug["decisions"]
    assert (
        f"{DECISION_PREFIX}: consent check for Margaret Chen via David Chen (approved, check 2) -> RESOLVE_INTENT"
        in decisions
    )
    assert any(d.startswith("RESOLVE_INTENT") and "CL-2048" in d for d in decisions)
    assert "CL-2048" in result.reply
    directive = _directive(result)
    assert directive["phase"] == "PROCESS_CASE"
    assert directive["instructions"][0] == CALLER_CONTEXT_INSTRUCTIONS["just_approved"]
    assert directive["facts"]["caller"]["consent_status"] == "approved"
    assert harness.state.public_state()["policyholder_name"] == "Margaret Chen"
    assert harness.state.public_state()["consent_status"] == "approved"


def test_timeout_scenario_hands_off_after_five_pending_checks(make_harness):
    harness: Harness = _rep_harness(make_harness, "timeout", scripts={"weather": {"scope": "out_of_scope"}})
    _match(harness)
    # Checks 2-5 (the fixture has five pending entries); an off-topic turn advances consent too.
    messages = [WAITING, "Still waiting?", "How is the weather today?", "Anything yet?"]
    for index, message in enumerate(messages, start=2):
        result = harness.say(message)
        assert harness.state.consent_status == "pending", message
        assert harness.state.consent_checks == index, message
        assert harness.state.phase == "VERIFY_ID"
        assert harness.state.verified is False
        assert "CL-" not in result.reply
    assert harness.state.consent_checks == 5
    assert harness.state.off_topic_streak == 0

    final = harness.say("Hello? Has she approved?")
    state = harness.state
    assert state.consent_checks == 6
    assert state.consent_status == "timed_out"
    assert state.phase == "HUMAN_HANDOFF"
    assert state.verified is False
    assert state.handoff_note is not None
    assert CONSENT_TIMEOUT_REASON in state.handoff_note
    assert "David Chen" in state.handoff_note and "Margaret Chen" in state.handoff_note
    assert "Caller: representative David Chen (son) for Margaret Chen; consent: timed_out" in state.handoff_note
    decisions = final.debug["decisions"]
    assert f"{DECISION_PREFIX}: consent check for Margaret Chen via David Chen (timed_out, check 6) -> HUMAN_HANDOFF" in decisions
    assert f"HUMAN_HANDOFF: {CONSENT_TIMEOUT_REASON}" in decisions
    directive = _directive(final)
    assert directive["phase"] == "HUMAN_HANDOFF"
    assert OFFER_HUMAN_INSTRUCTION not in directive["instructions"]
    assert "scope" not in directive["facts"]
    assert "caller_context" not in directive["facts"]
    assert not any("unrelated to insurance claims" in i for i in directive["instructions"])
    assert directive["tone"]

    queued = harness.say("Hello?")
    assert queued.reply == HANDOFF_QUEUE_REPLY
    assert queued.debug is not None and queued.debug["llm_calls"] == []
    assert harness.state.consent_checks == 6


def test_wants_human_during_pending_does_not_advance_consent(make_harness):
    harness: Harness = _rep_harness(make_harness, "timeout", scripts={"real person": {"wants_human": True}})
    _match(harness)
    result = harness.say("Just get me a real person")
    assert harness.state.phase == "HUMAN_HANDOFF"
    assert harness.state.consent_checks == 1
    assert harness.state.consent_status == "pending"
    assert harness.state.handoff_note is not None
    assert "caller requested a human representative" in harness.state.handoff_note
    assert "consent: pending" in harness.state.handoff_note
    assert result.debug is not None and result.debug["directive"]["phase"] == "HUMAN_HANDOFF"


class _FailingExtractorLLM(FakeLLM):
    def __init__(self, scripts: dict[str, dict[str, Any]], error: LLMError):
        super().__init__(scripts)
        self.error: LLMError | None = error

    def complete_json(self, system: str, messages: list, *, max_tokens: int = 700) -> dict[str, Any]:
        if self.error is not None and '"subject"' not in system:
            error, self.error = self.error, None
            raise error
        return super().complete_json(system, messages, max_tokens=max_tokens)


def test_extractor_error_during_pending_leaves_consent_unchanged(make_harness, monkeypatch: pytest.MonkeyPatch):
    harness: Harness = _rep_harness(make_harness, "timeout")
    _match(harness)
    original = harness.llm.complete_json
    calls = {"failed": False}

    def failing(system: str, messages: list, *, max_tokens: int = 700) -> dict[str, Any]:
        if not calls["failed"]:
            calls["failed"] = True
            raise LLMRequestError(503, "upstream unavailable")
        return original(system, messages, max_tokens=max_tokens)

    monkeypatch.setattr(harness.llm, "complete_json", failing)
    result = harness.say("Hello, any news?")
    assert calls["failed"] is True
    assert harness.state.consent_checks == 1
    assert harness.state.consent_status == "pending"
    assert harness.state.phase == "VERIFY_ID"
    assert result.debug is not None
    assert "extractor failed: templated fallback, state unchanged" in result.debug["decisions"]
    assert "David Chen" in result.reply and "Margaret Chen" in result.reply
    assert "CL-" not in result.reply

    # Recovery: the next successful turn is check 2.
    harness.say(WAITING)
    assert harness.state.consent_checks == 2


def test_extractor_error_before_match_uses_identify_fallback(tmp_path):
    from app.sop.controller import Controller
    from app.sop.trace import Tracer
    from app.tools.data import FixtureStore
    from tests.conftest import FIXTURES_DIR, make_settings

    settings = make_settings(tmp_path)
    llm = _FailingExtractorLLM(
        {"my mom": {"caller_role": "representative", "representative_name": "David Chen", "relationship": "son"}},
        LLMError("boom"),
    )
    controller = Controller(settings, FixtureStore(FIXTURES_DIR), llm, Tracer(settings.trace_enabled, settings.traces_dir))
    harness = Harness(controller, llm)

    # Extractor fails before any role is known: the normal VERIFY_ID fallback, state untouched.
    first = harness.say("Hi, David Chen here, calling about my mom's claim")
    assert harness.state.caller_role == "policyholder"
    assert "verify your identity" in first.reply

    harness.say("Hi, David Chen here, calling about my mom's claim")
    assert harness.state.caller_role == "representative"
    assert harness.state.representative is not None and harness.state.representative.party_id is None

    # Extractor fails again while the representative is still being identified: rep-specific fallback.
    llm.error = LLMError("boom again")
    fallback = harness.say("Did you get that?")
    assert "calling on behalf of someone else" in fallback.reply
    assert harness.state.caller_role == "representative"
    assert harness.state.verify_attempts == 0


def test_impersonation_with_policyholder_factors_does_not_verify(make_harness):
    harness: Harness = make_harness(
        {
            "on behalf": {
                **REP_EXTRACTION,
                "dob": "1985-03-15",
                "id_last4": "4472",
                "name": "Margaret Chen",
            }
        }
    )
    result = harness.say("David Chen on behalf of my mother Margaret Chen, her DOB is 1985-03-15 and SSN last four 4472")
    state = harness.state
    assert state.caller_role == "representative"
    assert state.slots.provided() == []
    assert state.verified is False
    assert state.verified_factors == NO_FACTORS
    assert state.consent_status == "pending"
    serialized = json.dumps(_directive(result))
    assert "1985-03-15" not in serialized and "4472" not in serialized
    assert "1985" not in result.reply and "4472" not in result.reply


def test_match_turn_is_progress_and_pending_frustration_hands_off(make_harness):
    harness: Harness = _rep_harness(
        make_harness,
        "timeout",
        scripts={"on behalf of my mother": {**REP_EXTRACTION, "emotion": "frustrated"}, "ridiculous": {"emotion": "angry"}},
    )
    _match(harness)
    assert harness.state.frustration_streak == 0

    for turn in range(1, 3):
        result = harness.say("This is ridiculous, why is this taking so long")
        assert harness.state.frustration_streak == turn
        assert harness.state.phase == "VERIFY_ID"
        assert harness.state.consent_status == "pending"
        assert result.debug["directive"]["tone"].startswith("The caller is angry")
    harness.say("This is ridiculous!")
    assert harness.state.phase == "HUMAN_HANDOFF"
    assert harness.state.handoff_note is not None
    assert "3 frustrated turns without progress" in harness.state.handoff_note
    assert "consent: pending" in harness.state.handoff_note
    assert harness.state.consent_checks == 4


def test_leak_guard_blocks_claim_data_while_consent_pending(make_harness):
    def leaky(directive: dict[str, Any], strict: bool) -> str:
        return "Sure David, her claim CL-2048 was denied because the pathology report was missing."

    harness: Harness = _rep_harness(make_harness, "timeout", responder=leaky)
    result = _match(harness)
    assert "CL-2048" not in result.reply
    assert "pathology" not in result.reply.lower()
    assert result.debug["guard"] == {"checked": True, "violation": True, "regenerated": True, "fallback_used": True}
    assert harness.llm.text_calls == [False, True]
    assert "David Chen" in result.reply and "Margaret Chen" in result.reply


# --------------------------------------------------------------------------------------------------
# After approval: caller context and post-processing
# --------------------------------------------------------------------------------------------------


def test_post_approval_directives_carry_caller_context(make_harness):
    harness: Harness = make_harness(
        {
            "on behalf": {
                "caller_role": "representative",
                "representative_name": "David Chen",
                "relationship": "son",
                "on_behalf_of_name": "Margaret Chen",
            },
            "approved": {},
            "dental": {"case_hints": {"case_type": "dental"}, "intent_path": "status_inquiry"},
            "that's all": {"conversation_done": True},
            "yes please": {"email_consent": "yes"},
        }
    )
    harness.say("David Chen on behalf of my mother Margaret Chen")
    assert harness.state.consent_status == "pending"

    resolve = harness.say("Has she approved yet?")
    assert harness.state.phase == "RESOLVE_INTENT"
    assert harness.state.verified is True
    directive = _directive(resolve)
    assert directive["facts"]["caller"] == {
        "role": "representative",
        "representative_name": "David Chen",
        "relationship": "son",
        "on_behalf_of": "Margaret Chen",
        "consent_status": "approved",
    }
    # Approval turn: the responder is told to announce the approval first.
    assert directive["instructions"][0] == CALLER_CONTEXT_INSTRUCTIONS["just_approved"]
    assert CALLER_CONTEXT_INSTRUCTIONS["default"] in directive["instructions"]
    assert CALLER_CONTEXT_INSTRUCTIONS["POST_PROCESS"] not in directive["instructions"]
    assert len(directive["facts"]["claims"]) == 4

    process = harness.say("It's about her dental claim")
    assert harness.state.phase == "PROCESS_CASE"
    assert harness.state.active_case_id == "CL-1899"
    directive = _directive(process)
    assert directive["facts"]["caller"]["role"] == "representative"
    assert directive["facts"]["caller"]["consent_status"] == "approved"
    assert CALLER_CONTEXT_INSTRUCTIONS["default"] in directive["instructions"]
    assert CALLER_CONTEXT_INSTRUCTIONS["just_approved"] not in directive["instructions"]

    post = harness.say("Ok, that's all")
    assert harness.state.phase == "POST_PROCESS"
    directive = _directive(post)
    assert directive["facts"]["caller"]["on_behalf_of"] == "Margaret Chen"
    assert CALLER_CONTEXT_INSTRUCTIONS["default"] in directive["instructions"]
    assert CALLER_CONTEXT_INSTRUCTIONS["POST_PROCESS"] in directive["instructions"]
    assert directive["facts"]["masked_email_on_file"] == "m*****@email.com"
    assert "margaret@email.com" not in post.reply

    closed = harness.say("Yes please")
    assert harness.state.phase == "CLOSED"
    assert len(harness.state.outbox) == 1
    entry = harness.state.outbox[0]
    assert entry.to_masked == "m*****@email.com"
    assert "email_summary" in harness.llm.json_calls
    assert _directive(closed)["facts"]["caller"]["representative_name"] == "David Chen"


def test_post_approval_email_fallback_names_policyholder(make_harness, monkeypatch: pytest.MonkeyPatch):
    harness: Harness = _rep_harness(
        make_harness,
        scripts={"that's all": {"conversation_done": True}, "yes please": {"email_consent": "yes"}},
    )
    _match(harness)
    harness.say(WAITING)
    assert harness.state.phase == "PROCESS_CASE"
    harness.say("Ok, that's all")
    assert harness.state.phase == "POST_PROCESS"

    # Force the email drafter down the templated path so the body is deterministic.
    original = harness.llm.complete_json

    def no_email_model(system: str, messages: list, *, max_tokens: int = 700) -> dict[str, Any]:
        if '"subject"' in system:
            raise LLMError("email model down")
        return original(system, messages, max_tokens=max_tokens)

    monkeypatch.setattr(harness.llm, "complete_json", no_email_model)
    harness.say("Yes please")
    assert harness.state.phase == "CLOSED"
    assert len(harness.state.outbox) == 1
    entry = harness.state.outbox[0]
    assert entry.to_masked == "m*****@email.com"
    assert "Hello Margaret Chen" in entry.body
    assert "David" not in entry.body
    assert "CL-2048" in entry.body


def test_public_state_after_approval(make_harness):
    harness: Harness = _rep_harness(make_harness)
    _match(harness)
    harness.say(WAITING)
    public = harness.state.public_state()
    assert public["verified"] is True
    assert public["policyholder_name"] == "Margaret Chen"
    assert public["caller_role"] == "representative"
    assert public["representative_name"] == "David Chen"
    assert public["representative_relationship"] == "son"
    assert public["consent_status"] == "approved"
    assert public["verified_factors"] == NO_FACTORS
    assert "party_id" not in public and "representative" not in public


def test_trace_contains_rep_decision_and_schema_only_extraction(make_harness, tmp_path):
    harness: Harness = _rep_harness(make_harness)
    _match(harness)
    trace_file = tmp_path / "traces" / f"{harness.state.session_id}.jsonl"
    assert trace_file.exists()
    entry = json.loads(trace_file.read_text(encoding="utf-8").splitlines()[-1])
    assert any(d.startswith(f"{DECISION_PREFIX}:") for d in entry["debug"]["decisions"])
    assert set(entry["debug"]["extraction"]) == set(Extraction.model_fields)


def test_start_session_rejects_unknown_scenario(make_harness):
    harness: Harness = make_harness({})
    with pytest.raises(ValueError) as excinfo:
        harness.controller.start_session(consent_scenario="bogus")
    assert "default" in str(excinfo.value) and "timeout" in str(excinfo.value)
    state, _ = harness.controller.start_session(consent_scenario="timeout")
    assert state.consent_scenario == "timeout"
    assert "caller_role" not in state.public_state()
