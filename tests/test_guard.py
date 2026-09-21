"""Output guard rule engine: rules, invariants, controller wiring and the responder correction note."""

from __future__ import annotations

import itertools
import json
import re
from typing import Any

import pytest

from app.sop import prompts
from app.sop.extractor import normalize_extraction
from app.sop.guard import (
    ClaimLeakRule,
    FalseStatusRule,
    MissingHumanOfferRule,
    OutputGuard,
    UnstatedMismatchRule,
    Violation,
    WrongFactorCountRule,
)
from app.sop.phases.verify_id import _FORBIDDEN, _fallback
from app.sop.prompts import GUARD_CORRECTIONS, RESPONDER_CORRECTION_NOTE
from app.sop.responder import build_responder_system
from app.sop.state import Directive, Extraction, SessionState
from tests.conftest import Harness, echo_responder

FALSE_CLAIM = "Perfect, thanks Margaret. I've got you verified now."
LOOKING = "I'm looking at your account now."
CLEAN_MISMATCH = (
    "I haven't been able to verify you yet because some of the details didn't match our records. "
    "Could you share your date of birth or the phone number on file?"
)
# The four turns from trace 3a106799921c: one matched factor (name), a mismatch on every turn.
REPLAY_SCRIPTS: dict[str, dict[str, Any]] = {
    "margaret chen, dob": {"name": "Margaret Chen", "dob": "1990-01-01", "phone": "555-0000"},
    "4727": {"id_last4": "4727"},
    "chen@gmail.com": {"email": "chen@gmail.com", "scope": "in_scope_general"},
}


def _unverified() -> SessionState:
    return SessionState(session_id="test")


def _verified() -> SessionState:
    return SessionState(session_id="test", verified=True, phase="PROCESS_CASE")


def _verify_directive(mismatch: bool = False, offer_human: bool = False) -> Directive:
    return Directive(
        phase="VERIFY_ID",
        goal="g",
        facts={"identity_verified": False, "mismatch_this_turn": mismatch, "offer_human": offer_human},
        fallback_text="f",
    )


def _process_directive() -> Directive:
    return Directive(phase="PROCESS_CASE", goal="g", facts={"claim": {"case_id": "CL-2048"}}, fallback_text="f")


def _fill(template: str) -> str:
    return re.sub(r"\{\w+\}", "x", template)


# --------------------------------------------------------------------------------------------------
# FalseStatusRule.scan
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        FALSE_CLAIM,
        "I have you verified.",
        "That has you verified, Margaret.",
        "You're verified.",
        "You are now verified.",
        "You're now fully verified, thanks.",
        "You've been verified.",
        "You have been verified successfully.",
        "Great, verified now.",
        "Your identity is confirmed.",
        "Identity has been verified.",
        "Your identity was now verified.",
        "I've confirmed your identity.",
        "We have verified your identity, so let's continue.",
        LOOKING,
        "I'm pulling up your records.",
        "I've pulled up your file.",
        "Let me bring this up, I'm bringing up your claim.",
        "I have your claim open in front of me.",
        "I've got your records here.",
        "I have your file up now.",
        # Clause-bounded negation: the "not" belongs to the previous sentence.
        "Thanks, that's not a problem. You're verified now.",
        "I can't share details yet! You're verified now, so ask away.",
        # Verification claimed to be running on the agent's side (live run 1, turns 3 and 4).
        "Perfect, thanks Margaret. I have all the information I need. Let me get everything verified on my end.",
        "I'm just finishing up the verification process on my end, and then I'll have everything I need.",
        "I'm processing your verification right now, give me one moment.",
        "Perfect, thanks Margaret. I have what I need to complete verification on my end. While that processes, is there anything else?",
        "I need to let you know that I'm still working through verification on my end before I can discuss any claim details.",
        "I have everything I need, thanks.",
        "We've got all of the details we need.",
        "Perfect, thanks Margaret. I've got all the details now. Just to confirm, is that correct?",
    ],
)
def test_false_status_rule_catches_completed_state_claims(text: str):
    assert FalseStatusRule().scan(text, _verify_directive()), text


@pytest.mark.parametrize(
    "text",
    [
        "I need to verify your identity.",
        "once I've verified your identity I can help",
        "I haven't got you verified yet",
        "you're not verified yet",
        "your identity isn't verified",
        "until your identity is verified I can't discuss the claim",
        "I've located your account, I still need two more details",
        "let's try another factor",
        "After I have verified your identity we can look at the claim.",
        "I cannot say you are verified until two more details match.",
        "Before I can say your identity is confirmed, I need one more detail.",
        "I'm unable to confirm that you are verified at this point.",
        "I need two more details so I can get you verified.",
        "I need a bit more information to get you verified.",
        "When your identity is verified, I will pull up the claim.",
        # Legitimate pre-verification phrasing seen in live runs of the existing scenarios.
        "I don't have a record of the details yet on my end, so could you share your full name and policy number?",
        "I need one more piece of information to complete the verification.",
        "I need to verify your identity first, then I can help with the claim.",
        "You need to be verified before I can discuss anything.",
        "Would you like me to connect you with a human representative, or can we get your identity verified so I can assist?",
        "Once I have everything I need, I can look into it.",
        "I don't have all the information I need yet.",
        "Thanks Margaret. I've got your account here. Some of the details you gave didn't match our records.",
        CLEAN_MISMATCH,
    ],
)
def test_false_status_rule_passes_conditional_and_negated_phrasing(text: str):
    assert FalseStatusRule().scan(text, _verify_directive()) == [], text


def test_false_status_rule_hits_are_the_matched_phrases():
    hits = FalseStatusRule().scan("Perfect. I've got you verified now and I'm looking at your account.", _verify_directive())
    assert hits == ["got you verified", "looking at your account", "verified now"]


def _templated_strings() -> list[str]:
    strings = [
        _fill(value)
        for name, value in vars(prompts).items()
        if name.startswith("FALLBACK_VERIFY_") and isinstance(value, str)
    ]
    strings.extend(_fill(value) for value in prompts.FALLBACK_REPRESENTATIVE.values())
    strings.append(_fill(prompts.HANDOFF_QUEUE_REPLY))
    strings.append(_fill(prompts.GREETING))
    strings.append(_fill(prompts.FALLBACK_OFFER_HUMAN))
    assert len(strings) >= 9
    return strings


@pytest.mark.parametrize("text", _templated_strings())
def test_false_status_rule_never_fires_on_templated_pre_verification_text(text: str):
    assert FalseStatusRule().scan(text, _verify_directive()) == [], text


def test_false_status_fallbacks_are_clean_over_the_whole_matrix():
    rule = FalseStatusRule()
    for located, mismatch, offer_human in itertools.product([True, False], repeat=3):
        text = _fallback(located, mismatch, offer_human, 2, ["date of birth", "phone number on file"])
        assert rule.scan(text, _verify_directive()) == []


# --------------------------------------------------------------------------------------------------
# ClaimLeakRule: behavioural pin for the refactor (same hits the old LeakGuard.scan returned).
# --------------------------------------------------------------------------------------------------


def test_claim_leak_rule_scan_matches_old_leak_guard(store):
    rule = ClaimLeakRule(store)
    directive = _verify_directive()
    assert rule.scan("I see your claim CL-2048 was denied because the pathology report was missing.", directive) == [
        "CL-2048",
        "cl-2048",
        "pathology report",
    ]
    assert rule.scan(
        "Your healthcare claim was denied due to missing pathology report and office note; the billed "
        "amount was $1,450.00 and the appeal deadline is 2026-03-18.",
        directive,
    ) == ["$1,450.00", "2026-03-18", "450.00", "office note", "pathology report"]
    assert rule.scan(
        "Thanks. To finish verifying your identity I still need 2 more of the following: date of birth. "
        "Which of those can you share?",
        directive,
    ) == []


def test_forbidding_rules_apply_only_before_verification(store):
    for rule in (ClaimLeakRule(store), FalseStatusRule()):
        assert rule.applies(_unverified(), _verify_directive()) is True
        assert rule.applies(_verified(), _process_directive()) is False
    assert ClaimLeakRule.name == "claim_leak" and FalseStatusRule.name == "false_status"
    assert ClaimLeakRule(store).correction == GUARD_CORRECTIONS["claim_leak"]
    assert FalseStatusRule().correction == GUARD_CORRECTIONS["false_status"]


# --------------------------------------------------------------------------------------------------
# UnstatedMismatchRule and MissingHumanOfferRule: statements the directive requires
# --------------------------------------------------------------------------------------------------


def test_required_statement_rules_apply_from_directive_facts():
    state = _unverified()
    assert UnstatedMismatchRule().applies(state, _verify_directive(mismatch=True)) is True
    assert UnstatedMismatchRule().applies(state, _verify_directive(mismatch=False)) is False
    assert UnstatedMismatchRule().applies(_verified(), _process_directive()) is False
    assert MissingHumanOfferRule().applies(state, _verify_directive(offer_human=True)) is True
    assert MissingHumanOfferRule().applies(state, _verify_directive(offer_human=False)) is False
    assert MissingHumanOfferRule().applies(_verified(), _process_directive()) is False
    # A representative directive carries no mismatch_this_turn key: never applies.
    rep = Directive(phase="VERIFY_ID", goal="g", facts={"consent_status": "pending", "offer_human": False}, fallback_text="f")
    assert UnstatedMismatchRule().applies(state, rep) is False and MissingHumanOfferRule().applies(state, rep) is False
    assert UnstatedMismatchRule().correction == GUARD_CORRECTIONS["unstated_mismatch"]
    assert MissingHumanOfferRule().correction == GUARD_CORRECTIONS["missing_human_offer"]


@pytest.mark.parametrize(
    "text",
    [
        CLEAN_MISMATCH,
        "Some of the details you gave did not match our records.",
        "Some of the details didn't match what we have on file.",
        "I'm having trouble matching all the details on my end.",
        "I wasn't able to match those details.",
        "Unfortunately those details don't quite match our records.",
        "There was a mismatch with one of the details.",
        "A couple of the details you shared didn't line up with our records.",
        "I could not match everything you gave me.",
        prompts.FALLBACK_VERIFY_MISMATCH_PREFIX,
    ],
)
def test_unstated_mismatch_rule_accepts_a_stated_mismatch(text: str):
    assert UnstatedMismatchRule().scan(text, _verify_directive(mismatch=True)) == []


@pytest.mark.parametrize(
    "text",
    [
        FALSE_CLAIM,
        "Thanks Margaret. I still need one more detail to finish verifying your identity. Could you share your date of birth?",
        "I want to make sure I have the right information on file for you. Could you share your date of birth?",
        "Let's match your details. Could you share your date of birth?",
        prompts.FALLBACK_VERIFY_FACTORS.format(count=2, factors="date of birth"),
    ],
)
def test_unstated_mismatch_rule_flags_a_missing_statement(text: str):
    assert UnstatedMismatchRule().scan(text, _verify_directive(mismatch=True)) == ["mismatch not stated"]


def test_missing_human_offer_rule():
    rule, directive = MissingHumanOfferRule(), _verify_directive(offer_human=True)
    assert rule.scan("Or if you'd prefer, I can connect you with a human claims representative.", directive) == []
    assert rule.scan("Would you rather speak with a representative?", directive) == []
    assert rule.scan(prompts.FALLBACK_OFFER_HUMAN, directive) == []
    assert rule.scan("Thanks Margaret. I still need one more detail. Could you share your date of birth?", directive) == [
        "human representative not offered"
    ]


def test_required_statement_rules_accept_every_matching_fallback():
    for located, mismatch, offer_human in itertools.product([True, False], repeat=3):
        text = _fallback(located, mismatch, offer_human, 2, ["date of birth", "phone number on file"])
        directive = _verify_directive(mismatch, offer_human)
        if mismatch:
            assert UnstatedMismatchRule().scan(text, directive) == []
        if offer_human:
            assert MissingHumanOfferRule().scan(text, directive) == []


# --------------------------------------------------------------------------------------------------
# OutputGuard
# --------------------------------------------------------------------------------------------------


def test_output_guard_applicable_follows_verification(store):
    guard = OutputGuard.default(store)
    assert guard.applicable(_unverified(), _verify_directive()) is True
    assert guard.applicable(_verified(), _process_directive()) is False
    assert guard.check(FALSE_CLAIM + " Your claim CL-2048 was denied.", _verified(), _process_directive()) == []


def test_output_guard_returns_one_violation_per_firing_rule(store):
    guard = OutputGuard.default(store)
    clean = guard.check(CLEAN_MISMATCH, _unverified(), _verify_directive(mismatch=True))
    assert clean == []

    only_status = guard.check(FALSE_CLAIM, _unverified(), _verify_directive())
    assert [v.rule for v in only_status] == ["false_status"]
    assert only_status[0] == Violation(
        rule="false_status", hits=["got you verified", "verified now"], correction=GUARD_CORRECTIONS["false_status"]
    )

    both = guard.check("I've got you verified now. Your claim CL-2048 was denied.", _unverified(), _verify_directive())
    assert [v.rule for v in both] == ["claim_leak", "false_status"]
    assert both[0].hits == ["CL-2048", "cl-2048"]
    assert both[0].correction == GUARD_CORRECTIONS["claim_leak"]
    assert both[1].hits == ["got you verified", "verified now"]

    everything = guard.check(
        "I've got you verified now. Your claim CL-2048 was denied.",
        _unverified(),
        _verify_directive(mismatch=True, offer_human=True),
    )
    assert [v.rule for v in everything] == ["claim_leak", "false_status", "unstated_mismatch", "missing_human_offer"]
    assert everything[2].hits == ["mismatch not stated"] and everything[3].hits == ["human representative not offered"]

    # Same lie on a turn without a mismatch or human offer: only the forbidding rules fire.
    assert [v.rule for v in guard.check(FALSE_CLAIM, _unverified(), _verify_directive(mismatch=True, offer_human=True))] == [
        "false_status",
        "unstated_mismatch",
        "missing_human_offer",
    ]


def test_echo_responder_output_passes_the_guard_before_verification(store, make_harness):
    """The refactor must not push any existing echo-based test onto the fallback path."""
    guard = OutputGuard.default(store)

    harness: Harness = make_harness({"hello": {"name": "Margaret Chen"}})
    result = harness.say("Hello, this is Margaret Chen")
    assert result.debug["directive"]["phase"] == "VERIFY_ID"
    assert result.debug["directive"]["facts"]["identity_verified"] is False
    assert '"identity_verified": false' in result.reply
    assert guard.check(result.reply, harness.state, Directive.model_validate(result.debug["directive"])) == []
    assert result.debug["guard"] == {"checked": True, "violation": False, "regenerated": False, "fallback_used": False}

    # A mismatch turn with attempts exhausted: the echoed must_ask states the mismatch and the human offer.
    exhausted: Harness = make_harness(REPLAY_SCRIPTS, max_verify_attempts=1)
    result = exhausted.say("This is Margaret Chen, DOB 1990-01-01, phone 555-0000")
    facts = result.debug["directive"]["facts"]
    assert facts["mismatch_this_turn"] is True and facts["offer_human"] is True
    assert guard.check(result.reply, exhausted.state, Directive.model_validate(result.debug["directive"])) == []
    assert result.debug["guard"]["violation"] is False

    rep: Harness = make_harness(
        {
            "david chen": {
                "caller_role": "representative",
                "representative_name": "David Chen",
                "relationship": "son",
                "on_behalf_of_name": "Margaret Chen",
            }
        }
    )
    rep.state, _ = rep.controller.start_session("timeout")
    pending = rep.say("Hi, this is David Chen calling for my mother Margaret Chen")
    assert rep.state.consent_status == "pending"
    assert echo_responder(pending.debug["directive"], False) == pending.reply
    assert guard.check(pending.reply, rep.state, Directive.model_validate(pending.debug["directive"])) == []
    assert pending.debug["guard"]["fallback_used"] is False


# --------------------------------------------------------------------------------------------------
# Controller wiring through the harness
# --------------------------------------------------------------------------------------------------


def _lying_responder(directive: dict[str, Any], strict: bool) -> str:
    return FALSE_CLAIM


def _record_systems(harness: Harness) -> list[str]:
    """Capture every responder system prompt the FakeLLM receives, in call order."""
    systems: list[str] = []
    original = harness.llm.complete_text

    def recording(system: str, messages: list[Any], **kwargs: Any) -> str:
        systems.append(system)
        return original(system, messages, **kwargs)

    harness.llm.complete_text = recording  # type: ignore[method-assign]
    return systems


def test_false_verification_claim_is_blocked_and_fallback_is_honest(make_harness):
    """The 1-factor mismatch turn from the trace, with attempts already exhausted (max 1)."""
    harness: Harness = make_harness(REPLAY_SCRIPTS, responder=_lying_responder, max_verify_attempts=1)
    result = harness.say("This is Margaret Chen, DOB 1990-01-01, phone 555-0000")
    state = harness.state
    assert state.verified is False and state.phase == "VERIFY_ID"
    assert state.verified_factors["name"] is True and sum(state.verified_factors.values()) == 1
    assert result.debug["directive"]["facts"]["offer_human"] is True

    assert result.debug["guard"] == {"checked": True, "violation": True, "regenerated": True, "fallback_used": True}
    assert harness.llm.text_calls == [False, True]
    assert result.reply.startswith("Some of the details you gave did not match")
    assert "human claims representative" in result.reply
    assert "verified now" not in result.reply.lower()
    assert "guard[false_status]: blocked 2 hit(s), regenerating" in result.debug["decisions"]
    assert "guard: regenerated reply still unsafe, templated fallback used" in result.debug["decisions"]
    assert state.transcript[-1].role == "agent" and state.transcript[-1].text == result.reply
    assert not any("verified now" in turn.text for turn in state.transcript)


def test_trace_replay_third_turn_is_blocked_at_attempt_three(make_harness):
    """Same four turns as trace 3a106799921c; the model lies only on the exhausted-attempts turn."""

    def responder(directive: dict[str, Any], strict: bool) -> str:
        if directive["facts"].get("offer_human") and not strict:
            return FALSE_CLAIM
        return echo_responder(directive, strict)

    harness: Harness = make_harness(REPLAY_SCRIPTS, responder=responder)
    harness.say("This is Margaret Chen, DOB 1990-01-01, phone 555-0000")
    assert harness.state.verify_attempts == 1 and harness.state.verified is False
    harness.say("yeah 4727 and 8946748929")
    assert harness.state.verify_attempts == 2

    third = harness.say("chen@gmail.com")
    assert harness.state.verify_attempts == 3 and harness.state.verified is False
    # The scope normaliser turned in_scope_general into in_scope_claim: no scope instruction pile-up.
    assert third.debug["extraction"]["scope"] == "in_scope_claim"
    assert "scope" not in third.debug["directive"]["facts"]
    assert third.debug["guard"]["violation"] is True and third.debug["guard"]["fallback_used"] is False
    assert harness.llm.text_calls == [False, False, False, True]
    assert "verified now" not in third.reply.lower()
    assert '"identity_verified": false' in third.reply
    assert any("guard[false_status]" in d for d in third.debug["decisions"])

    fourth = harness.say("tell me my claim denial reason")
    assert harness.state.verified is False and harness.state.off_topic_streak == 0
    assert "CL-" not in fourth.reply


def test_looking_at_account_blocked_before_verification_only(make_harness, verified_harness: Harness):
    def looking(directive: dict[str, Any], strict: bool) -> str:
        return LOOKING

    harness: Harness = make_harness({"hello": {"name": "Margaret Chen"}}, responder=looking)
    result = harness.say("Hello, this is Margaret Chen")
    assert result.debug["guard"]["violation"] is True
    assert "looking at your account" not in result.reply.lower()
    assert any("guard[false_status]" in d for d in result.debug["decisions"])

    verified_harness.llm.responder = looking
    after = verified_harness.say("Can you check the status?")
    assert verified_harness.state.phase == "PROCESS_CASE"
    assert after.reply == LOOKING
    assert after.debug["guard"] == {"checked": False, "violation": False, "regenerated": False, "fallback_used": False}


def test_clean_regeneration_is_used_and_carries_the_correction(make_harness):
    def responder(directive: dict[str, Any], strict: bool) -> str:
        return CLEAN_MISMATCH if strict else FALSE_CLAIM

    harness: Harness = make_harness(REPLAY_SCRIPTS, responder=responder)
    systems = _record_systems(harness)
    result = harness.say("This is Margaret Chen, DOB 1990-01-01, phone 555-0000")

    assert result.reply == CLEAN_MISMATCH
    assert result.debug["guard"] == {"checked": True, "violation": True, "regenerated": True, "fallback_used": False}
    assert harness.llm.text_calls == [False, True]
    assert len(systems) == 2
    assert "CRITICAL CORRECTION" not in systems[0]
    assert "CRITICAL CORRECTION" in systems[1]
    assert GUARD_CORRECTIONS["false_status"] in systems[1]
    assert GUARD_CORRECTIONS["claim_leak"] not in systems[1]
    assert harness.state.transcript[-1].text == CLEAN_MISMATCH


def test_both_rules_firing_yields_two_decisions_and_two_corrections(make_harness):
    def responder(directive: dict[str, Any], strict: bool) -> str:
        return CLEAN_MISMATCH if strict else "I've got you verified now. Your claim CL-2048 was denied."

    harness: Harness = make_harness({"hello": {"name": "Margaret Chen"}}, responder=responder)
    systems = _record_systems(harness)
    result = harness.say("Hello, this is Margaret Chen")
    decisions = result.debug["decisions"]
    assert "guard[claim_leak]: blocked 2 hit(s), regenerating" in decisions
    assert "guard[false_status]: blocked 2 hit(s), regenerating" in decisions
    assert not any("CL-2048" in d for d in decisions)
    assert GUARD_CORRECTIONS["claim_leak"] in systems[1] and GUARD_CORRECTIONS["false_status"] in systems[1]
    assert result.reply == CLEAN_MISMATCH


# --------------------------------------------------------------------------------------------------
# Responder correction rendering
# --------------------------------------------------------------------------------------------------


def _directive() -> Directive:
    return Directive(phase="VERIFY_ID", goal="g", facts={"identity_verified": False}, fallback_text="f")


def test_build_responder_system_renders_each_correction_once():
    system = build_responder_system(_directive(), corrections=["alpha broke", "beta broke"])
    assert system.count("CRITICAL CORRECTION") == 1
    assert "- alpha broke\n- beta broke" in system
    assert system.endswith(RESPONDER_CORRECTION_NOTE.format(items="- alpha broke\n- beta broke"))


def test_build_responder_system_without_corrections_has_no_correction_block():
    plain = build_responder_system(_directive())
    assert plain == build_responder_system(_directive(), corrections=None) == build_responder_system(_directive(), corrections=[])
    assert "CRITICAL CORRECTION" not in plain
    assert plain.rstrip().endswith("}")
    assert json.loads(plain[plain.index("DIRECTIVE:") + len("DIRECTIVE:") :])["facts"]["identity_verified"] is False


# --------------------------------------------------------------------------------------------------
# normalize_extraction: identity data implies in_scope_claim
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"email": "chen@gmail.com", "scope": "in_scope_general"}, "in_scope_claim"),
        ({"dob": "1990-01-01", "scope": "out_of_scope"}, "in_scope_claim"),
        ({"scope": "out_of_scope"}, "out_of_scope"),
        ({"scope": "in_scope_general"}, "in_scope_general"),
        ({"scope": "small_talk"}, "small_talk"),
    ],
)
def test_normalize_extraction_identity_implies_in_scope(data: dict[str, Any], expected: str):
    assert normalize_extraction(Extraction.model_validate(data)).scope == expected


def test_out_of_scope_labelled_dob_turn_does_not_count_as_off_topic(make_harness):
    harness: Harness = make_harness(
        {"what is rl": {"scope": "out_of_scope"}, "1985": {"dob": "1985-03-15", "scope": "out_of_scope"}}
    )
    harness.say("What is RL?")
    assert harness.state.off_topic_streak == 1
    result = harness.say("My date of birth is 1985-03-15")
    assert harness.state.off_topic_streak == 0
    assert result.debug["extraction"]["scope"] == "in_scope_claim"
    assert "scope" not in result.debug["directive"]["facts"]
    assert not any(d.startswith("scope: out_of_scope") for d in result.debug["decisions"])


# --------------------------------------------------------------------------------------------------
# VERIFY_ID directive honesty
# --------------------------------------------------------------------------------------------------


def test_verify_id_directive_carries_identity_verified_false_and_forbids_status_claims(make_harness):
    harness: Harness = make_harness({"hello": {"name": "Margaret Chen"}})
    result = harness.say("Hello, this is Margaret Chen")
    directive = result.debug["directive"]
    assert directive["facts"]["identity_verified"] is False
    assert any(item.startswith("Saying or implying the caller is verified") for item in directive["forbidden"])
    assert directive["forbidden"] == _FORBIDDEN
    assert "never be the one to decide verification is complete" in prompts.PHASE_GUIDANCE["VERIFY_ID"]


@pytest.mark.parametrize(
    ("located", "mismatch", "offer_human"),
    list(itertools.product([True, False], repeat=3)),
)
def test_fallback_matrix(located: bool, mismatch: bool, offer_human: bool):
    needed = ["date of birth", "phone number on file"]
    text = _fallback(located, mismatch, offer_human, 2, needed)
    body = (
        prompts.FALLBACK_VERIFY_FACTORS.format(count=2, factors=", ".join(needed))
        if located
        else prompts.FALLBACK_VERIFY_LOCATE.format(company_name=prompts.COMPANY_NAME)
    )
    assert body in text
    assert text.startswith(prompts.FALLBACK_VERIFY_MISMATCH_PREFIX) is (mismatch and located)
    assert text.startswith(prompts.FALLBACK_VERIFY_NOT_FOUND_PREFIX) is (mismatch and not located)
    assert text.startswith(body) is (not mismatch)
    assert text.endswith(prompts.FALLBACK_OFFER_HUMAN) is offer_human
    assert text.endswith(body) is (not offer_human)
    assert ("human claims representative" in text) is offer_human


def test_directive_fallback_matches_directive_facts(make_harness):
    harness: Harness = make_harness({"nobody": {"name": "Nobody Real", "policy_number": "POL-0000"}})
    for _ in range(3):
        result = harness.say("I'm Nobody Real, policy POL-0000")
    directive = result.debug["directive"]
    assert directive["facts"] == {
        "identity_verified": False,
        "account_located": False,
        "matched_factor_count": 0,
        "factors_still_needed_count": 3,
        "acceptable_factors": directive["facts"]["acceptable_factors"],
        "mismatch_this_turn": True,
        "offer_human": True,
    }
    assert directive["fallback_text"].startswith(prompts.FALLBACK_VERIFY_NOT_FOUND_PREFIX)
    assert directive["fallback_text"].endswith(prompts.FALLBACK_OFFER_HUMAN)


# --------------------------------------------------------------------------------------------------
# WrongFactorCountRule: a stated remaining count must equal facts.factors_still_needed_count
# --------------------------------------------------------------------------------------------------

# Live replay turns 3 and 4: the model counted down although nothing was accepted (two factors remain).
WRONG_COUNT = (
    "Thanks. Some details didn't match. I still need 1 more: your email or the last 4. "
    "Or I can connect you with a human representative."
)
RIGHT_COUNT = WRONG_COUNT.replace("1 more", "2 more")
ALL_LABELS = ["date of birth", "phone number on file", "email address on file", "last 4 of your SSN or government ID"]


def _count_directive(remaining: int, located: bool = True, mismatch: bool = False, offer_human: bool = False) -> Directive:
    """A VERIFY_ID directive carrying the full facts set verify_id._directive emits."""
    return Directive(
        phase="VERIFY_ID",
        goal="g",
        facts={
            "identity_verified": False,
            "account_located": located,
            "matched_factor_count": 3 - remaining,
            "factors_still_needed_count": remaining,
            "acceptable_factors": ALL_LABELS,
            "mismatch_this_turn": mismatch,
            "offer_human": offer_human,
        },
        fallback_text="f",
    )


@pytest.mark.parametrize(
    ("text", "expected", "hits"),
    [
        (WRONG_COUNT, 2, ["stated 1, expected 2"]),
        ("I still need 1 more", 2, ["stated 1, expected 2"]),
        ("I just need one last detail from you.", 2, ["stated 1, expected 2"]),
        ("I need just one of the following: your date of birth or the phone number on file.", 2, ["stated 1, expected 2"]),
        ("I need only 1 to finish.", 2, ["stated 1, expected 2"]),
        ("I need 2 pieces of information to finish verifying you.", 1, ["stated 2, expected 1"]),
        ("I still need two more details.", 1, ["stated 2, expected 1"]),
        ("Three additional details are needed.", 2, ["stated 3, expected 2"]),
        # Filler is skipped but the real count next to it is still read.
        ("Give me one moment. I need two more details.", 1, ["stated 2, expected 1"]),
        # Several distinct wrong counts are each reported once.
        ("I need 1 more, or three further details if you prefer. Just 1 more.", 2, ["stated 1, expected 2", "stated 3, expected 2"]),
    ],
)
def test_wrong_factor_count_rule_flags_a_count_that_differs_from_the_directive(text: str, expected: int, hits: list[str]):
    assert WrongFactorCountRule().scan(text, _count_directive(expected)) == hits


@pytest.mark.parametrize(
    "text",
    [
        RIGHT_COUNT,
        "I still need 2 more: your date of birth or the phone number on file.",
        "I need two more of the following.",
        "One more thing: could you share your date of birth?",
        "You have one more attempt before I transfer you to a human representative.",
        "Let's give it one last try.",
        "Any one of these will do: your date of birth or the last 4 of your SSN.",
        "You need any one of these.",
        "Could you share the last 4 of your SSN or government ID?",
        "Give me one moment while I note that.",
        "I have one question for you.",
        "Some of the details didn't match our records. Could you share your date of birth?",
        CLEAN_MISMATCH,
        prompts.FALLBACK_OFFER_HUMAN,
    ],
)
def test_wrong_factor_count_rule_passes_correct_counts_filler_and_no_count(text: str):
    assert WrongFactorCountRule().scan(text, _count_directive(2)) == [], text


def test_wrong_factor_count_rule_applies_only_to_located_verify_id_directives():
    rule, state = WrongFactorCountRule(), _unverified()
    assert rule.name == "wrong_factor_count" and rule.correction == GUARD_CORRECTIONS["wrong_factor_count"]
    assert rule.applies(state, _count_directive(2)) is True
    assert rule.applies(state, _count_directive(3, located=False)) is False
    rep = Directive(phase="VERIFY_ID", goal="g", facts={"consent_status": "pending", "offer_human": False}, fallback_text="f")
    assert rule.applies(state, rep) is False
    assert rule.applies(_verified(), _process_directive()) is False
    # Every earlier test in this file builds its directive with _verify_directive, which carries no
    # account_located or factors_still_needed_count key: the rule never applies there, by construction.
    assert rule.applies(state, _verify_directive()) is False
    assert rule.applies(state, _verify_directive(mismatch=True, offer_human=True)) is False


def test_output_guard_default_runs_wrong_factor_count_last(store):
    guard = OutputGuard.default(store)
    directive = _count_directive(2, mismatch=True, offer_human=True)
    everything = guard.check("I've got you verified now. I still need 1 more.", _unverified(), directive)
    assert [v.rule for v in everything] == ["false_status", "unstated_mismatch", "missing_human_offer", "wrong_factor_count"]
    assert everything[-1] == Violation(
        rule="wrong_factor_count", hits=["stated 1, expected 2"], correction=GUARD_CORRECTIONS["wrong_factor_count"]
    )
    assert [v.rule for v in guard.check(WRONG_COUNT, _unverified(), directive)] == ["wrong_factor_count"]
    assert guard.check(RIGHT_COUNT, _unverified(), directive) == []


@pytest.mark.parametrize("count", [1, 2, 3])
def test_fallback_verify_factors_states_the_directive_count(count: int):
    """Invariant: the templated fallback always agrees with the facts it was built from."""
    rule = WrongFactorCountRule()
    body = prompts.FALLBACK_VERIFY_FACTORS.format(count=count, factors=", ".join(ALL_LABELS))
    assert rule.scan(body, _count_directive(count)) == []
    for mismatch, offer_human in itertools.product([True, False], repeat=2):
        text = _fallback(True, mismatch, offer_human, count, ALL_LABELS)
        assert rule.scan(text, _count_directive(count, mismatch=mismatch, offer_human=offer_human)) == []
    # The invariant is not vacuous: the same text against a directive expecting another count fires.
    other = 1 if count != 1 else 2
    assert rule.scan(body, _count_directive(other)) == [f"stated {count}, expected {other}"]


def test_echo_responder_states_the_directive_count(store, make_harness):
    """The echoed must_ask ("ask for N more of these") carries the correct count for N in {1, 2}."""
    guard = OutputGuard.default(store)

    two: Harness = make_harness(REPLAY_SCRIPTS)
    result = two.say("This is Margaret Chen, DOB 1990-01-01, phone 555-0000")
    facts = result.debug["directive"]["facts"]
    assert facts["account_located"] is True and facts["factors_still_needed_count"] == 2
    assert "ask for 2 more of these" in result.reply.lower()
    assert guard.check(result.reply, two.state, Directive.model_validate(result.debug["directive"])) == []
    assert result.debug["guard"] == {"checked": True, "violation": False, "regenerated": False, "fallback_used": False}

    one: Harness = make_harness({"hello": {"name": "Margaret Chen", "dob": "1985-03-15"}})
    result = one.say("Hello, this is Margaret Chen, born 1985-03-15")
    facts = result.debug["directive"]["facts"]
    assert facts["matched_factor_count"] == 2 and facts["factors_still_needed_count"] == 1
    assert "ask for 1 more of these" in result.reply.lower()
    assert guard.check(result.reply, one.state, Directive.model_validate(result.debug["directive"])) == []
    assert result.debug["guard"]["violation"] is False


def test_wrong_count_is_blocked_and_the_corrected_count_is_accepted(make_harness):
    """The replay turn: two factors remain, the model says "1 more", the regenerated draft says "2 more"."""

    def responder(directive: dict[str, Any], strict: bool) -> str:
        return RIGHT_COUNT if strict else WRONG_COUNT

    harness: Harness = make_harness(REPLAY_SCRIPTS, responder=responder)
    systems = _record_systems(harness)
    result = harness.say("This is Margaret Chen, DOB 1990-01-01, phone 555-0000")
    facts = result.debug["directive"]["facts"]
    assert facts["factors_still_needed_count"] == 2 and facts["mismatch_this_turn"] is True

    assert result.debug["guard"] == {"checked": True, "violation": True, "regenerated": True, "fallback_used": False}
    assert [d for d in result.debug["decisions"] if d.startswith("guard[")] == [
        "guard[wrong_factor_count]: blocked 1 hit(s), regenerating"
    ]
    assert harness.llm.text_calls == [False, True]
    assert "CRITICAL CORRECTION" not in systems[0]
    assert GUARD_CORRECTIONS["wrong_factor_count"] in systems[1]
    assert result.reply == RIGHT_COUNT
    assert harness.state.transcript[-1].text == RIGHT_COUNT
    assert not any("1 more" in turn.text for turn in harness.state.transcript)


def test_correct_count_passes_the_guard_without_regeneration(make_harness):
    def responder(directive: dict[str, Any], strict: bool) -> str:
        return RIGHT_COUNT

    harness: Harness = make_harness(REPLAY_SCRIPTS, responder=responder)
    result = harness.say("This is Margaret Chen, DOB 1990-01-01, phone 555-0000")
    assert result.debug["directive"]["facts"]["factors_still_needed_count"] == 2
    assert result.debug["guard"] == {"checked": True, "violation": False, "regenerated": False, "fallback_used": False}
    assert harness.llm.text_calls == [False]
    assert not any(d.startswith("guard[") for d in result.debug["decisions"])
    assert result.reply == RIGHT_COUNT
