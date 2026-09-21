"""Soft stop on identity verification: attempt == max offers a human, the next mismatch transfers.

``MAX_VERIFY_ATTEMPTS`` keeps its meaning ("mismatching attempts before the human offer"). The
attempt after that is a grace attempt: a partial match keeps the caller in VERIFY_ID, another
mismatch hands off automatically through the same path the representative consent timeout uses.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from app.config import Settings
from app.sop.phases import verification_exhausted
from app.sop.phases.representative import DECISION_PREFIX
from app.sop.prompts import (
    COMPANY_NAME,
    FALLBACK_HANDOFF,
    FALLBACK_OFFER_HUMAN,
    FALLBACK_REPRESENTATIVE,
    FALLBACK_VERIFY_FACTORS,
    FALLBACK_VERIFY_LOCATE,
    FALLBACK_VERIFY_MISMATCH_PREFIX,
    FALLBACK_VERIFY_NOT_FOUND_PREFIX,
    HANDOFF_QUEUE_REPLY,
    OFFER_HUMAN_INSTRUCTION,
)
from app.sop.state import FACTOR_LABELS, SessionState
from tests.conftest import Harness, make_settings

# Margaret Chen (P9) is located by name on turn 1; every other detail is wrong, so each turn is one
# mismatching attempt while the matched name keeps the account located.
WRONG_DETAIL_SCRIPTS: dict[str, dict[str, Any]] = {
    "margaret chen, dob 1990": {"name": "Margaret Chen", "dob": "1990-01-01"},
    "555-0000": {"phone": "555-0000"},
    "last four 0000": {"id_last4": "0000"},
    "nobody@example.com": {"email": "nobody@example.com"},
    "dob 1985-03-15": {"dob": "1985-03-15"},
    "refuse": {"emotion": "refusing"},
}
WRONG_DETAIL_TURNS = [
    "This is Margaret Chen, DOB 1990-01-01",
    "My phone is 555-0000",
    "Last four 0000",
    "My email is nobody@example.com",
]
REP_SCRIPTS: dict[str, dict[str, Any]] = {
    "bob jones": {
        "caller_role": "representative",
        "representative_name": "Bob Jones",
        "relationship": "nephew",
        "on_behalf_of_name": "Margaret Chen",
    }
}
NOT_FOUND_SCRIPTS: dict[str, dict[str, Any]] = {"nobody real": {"name": "Nobody Real", "policy_number": "POL-0000"}}

IDENTITY_REASON = "identity not verified after 4 attempts"
REP_REASON = "representative authorization not found after 4 attempts"
VERIFY_ID_ONLY_RULES = ("unstated_mismatch", "missing_human_offer", "wrong_factor_count")

# The offer turn exactly as it stands today (one factor matched, two still needed, mismatch this turn).
EXPECTED_NEEDED = [FACTOR_LABELS[f] for f in ("dob", "phone", "email", "id_last4")]
EXPECTED_OFFER_MUST_ASK = (
    "Say plainly that some of the details given did not match our records, then ask for 2 more of these, "
    f"offering them as alternatives: {', '.join(EXPECTED_NEEDED)}. "
    "Offer to connect them with a human claims representative as an alternative."
)
EXPECTED_OFFER_INSTRUCTIONS = [
    "Identity is NOT verified. Do not reference any claim in any way.",
    "If the caller asks why verification is needed, explain that it protects their claim information, then repeat the ask.",
    "Some details did not match our records, so nothing new was accepted this turn: say so generically and "
    "invite them to try again or use another factor. Never say which detail, and never soften it into a vague check.",
    "Verification is not progressing: offer to connect them with a human claims representative while leaving the door open to continue.",
]
EXPECTED_OFFER_FACTS = {
    "identity_verified": False,
    "account_located": True,
    "matched_factor_count": 1,
    "factors_still_needed_count": 2,
    "acceptable_factors": EXPECTED_NEEDED,
    "mismatch_this_turn": True,
    "offer_human": True,
}
EXPECTED_OFFER_FALLBACK = (
    FALLBACK_VERIFY_MISMATCH_PREFIX
    + FALLBACK_VERIFY_FACTORS.format(count=2, factors=", ".join(EXPECTED_NEEDED))
    + FALLBACK_OFFER_HUMAN
)


def _directive(result) -> dict[str, Any]:
    assert result.debug is not None and result.debug["directive"] is not None
    return result.debug["directive"]


def _play_wrong_details(harness: Harness, turns: int):
    result = None
    for message in WRONG_DETAIL_TURNS[:turns]:
        result = harness.say(message)
    assert result is not None
    return result


def _assert_handoff_directive(result, reason: str) -> None:
    directive = _directive(result)
    assert directive["phase"] == "HUMAN_HANDOFF"
    assert directive["facts"] == {"reason": reason, "verified": False}
    assert directive["fallback_text"] == FALLBACK_HANDOFF
    assert "Identity is not verified: do not mention any claim information." in directive["instructions"]
    # A handler transferred the caller itself: _decide returns before scope/memory decoration.
    assert OFFER_HUMAN_INSTRUCTION not in directive["instructions"]
    assert "scope" not in directive["facts"] and "caller_context" not in directive["facts"]
    assert not any("unrelated to insurance claims" in i for i in directive["instructions"])
    assert directive["tone"]
    assert f"HUMAN_HANDOFF: {reason}" in result.debug["decisions"]


# --------------------------------------------------------------------------------------------------
# Predicate
# --------------------------------------------------------------------------------------------------


def test_verification_exhausted_is_strictly_beyond_max(tmp_path):
    settings: Settings = make_settings(tmp_path)
    assert settings.max_verify_attempts == 3
    state = SessionState(session_id="s")
    for attempts in range(0, 4):
        state.verify_attempts = attempts
        assert verification_exhausted(state, settings) is False, attempts
    state.verify_attempts = 4
    assert verification_exhausted(state, settings) is True

    one: Settings = make_settings(tmp_path, max_verify_attempts=1)
    state.verify_attempts = 1
    assert verification_exhausted(state, one) is False
    state.verify_attempts = 2
    assert verification_exhausted(state, one) is True


# --------------------------------------------------------------------------------------------------
# Policyholder, located account (mismatch after the name matched)
# --------------------------------------------------------------------------------------------------


def test_three_mismatches_only_offer_and_the_offer_turn_is_unchanged(make_harness: Callable[..., Harness]):
    harness = make_harness(WRONG_DETAIL_SCRIPTS)
    for turn in range(1, 4):
        result = harness.say(WRONG_DETAIL_TURNS[turn - 1])
        state = harness.state
        assert state.phase == "VERIFY_ID", turn
        assert state.verified is False
        assert state.verify_attempts == turn
        assert state.verified_factors["name"] is True
        assert sum(state.verified_factors.values()) == 1
        assert _directive(result)["facts"]["offer_human"] is (turn == 3), turn
        assert state.handoff_note is None

    offer = _directive(result)
    assert offer["phase"] == "VERIFY_ID"
    assert offer["goal"] == "Verify the caller's identity with three matching factors before anything else."
    assert offer["must_ask"] == EXPECTED_OFFER_MUST_ASK
    assert offer["instructions"] == EXPECTED_OFFER_INSTRUCTIONS
    assert offer["facts"] == EXPECTED_OFFER_FACTS
    assert offer["fallback_text"] == EXPECTED_OFFER_FALLBACK
    # The caller is not warned that the next failure transfers them: the grace attempt is internal.
    serialized = json.dumps(offer).lower()
    for word in ("last try", "final attempt", "one more attempt", "transfer", "hand off", "handoff"):
        assert word not in serialized, word


def test_fourth_mismatch_hands_off_silently(make_harness: Callable[..., Harness]):
    harness = make_harness(WRONG_DETAIL_SCRIPTS)
    _play_wrong_details(harness, 3)
    assert harness.state.phase == "VERIFY_ID"

    fourth = harness.say(WRONG_DETAIL_TURNS[3])
    state = harness.state
    assert state.phase == "HUMAN_HANDOFF"
    assert state.verified is False
    assert state.party_id is None and state.policyholder_name is None
    assert state.verify_attempts == 4
    assert state.handoff_note is not None
    assert "after 4 attempts" in state.handoff_note
    assert state.handoff_note.startswith(f"Reason: {IDENTITY_REASON}; Identity verified: no; ")
    assert "Caller: representative" not in state.handoff_note
    _assert_handoff_directive(fourth, IDENTITY_REASON)
    decisions = fourth.debug["decisions"]
    assert not any(d.startswith("VERIFY_ID:") for d in decisions), decisions
    assert "CL-" not in fourth.reply and "1990" not in fourth.reply and "0000" not in fourth.reply
    assert "nobody@example.com" not in json.dumps(_directive(fourth))

    public = state.public_state()
    assert public["phase"] == "HUMAN_HANDOFF"
    assert public["verify_attempts"] == 4
    assert public["verified"] is False
    assert public["handoff_note"] == state.handoff_note
    assert "1990-01-01" not in json.dumps(public) and "555-0000" not in json.dumps(public)

    # Terminal: the fixed queue reply, no model calls, counters frozen.
    queued = harness.say("Hello? Are you still there?")
    assert queued.reply == HANDOFF_QUEUE_REPLY
    assert queued.debug is not None and queued.debug["llm_calls"] == []
    assert queued.debug["directive"] is None
    assert queued.debug["decisions"] == ["HUMAN_HANDOFF: terminal, fixed queue reply"]
    assert harness.state.phase == "HUMAN_HANDOFF"
    assert harness.state.verify_attempts == 4
    assert len(harness.llm.json_calls) == 4 and len(harness.llm.text_calls) == 4


def test_guard_on_the_handoff_turn_keeps_leak_rules_and_drops_verify_only_rules(make_harness: Callable[..., Harness]):
    def leaky(directive: dict[str, Any], strict: bool) -> str:
        if directive["phase"] == "HUMAN_HANDOFF" and not strict:
            # Says nothing about a mismatch or a human offer (would trip the VERIFY_ID-only rules), but
            # leaks claim data and a false status, which still apply to an unverified caller.
            return "You're verified now. Your claim CL-2048 was denied; I am connecting you with a human claims representative."
        if directive["phase"] == "HUMAN_HANDOFF":
            return "I understand. I am connecting you with a human claims representative who can take it from here."
        return "Some of the details did not match. Please try another factor. I can also connect you with a human representative."

    harness = make_harness(WRONG_DETAIL_SCRIPTS, responder=leaky)
    fourth = _play_wrong_details(harness, 4)
    assert harness.state.phase == "HUMAN_HANDOFF"
    assert _directive(fourth)["phase"] == "HUMAN_HANDOFF"
    guard = fourth.debug["guard"]
    assert guard["checked"] is True
    assert guard["violation"] is True and guard["regenerated"] is True and guard["fallback_used"] is False
    fired = [d for d in fourth.debug["decisions"] if d.startswith("guard[")]
    assert any(d.startswith("guard[claim_leak]") for d in fired), fired
    assert any(d.startswith("guard[false_status]") for d in fired), fired
    for rule in VERIFY_ID_ONLY_RULES:
        assert not any(f"guard[{rule}]" in d for d in fourth.debug["decisions"]), rule
    assert "CL-2048" not in fourth.reply and "verified now" not in fourth.reply
    assert harness.llm.text_calls[-2:] == [False, True]


def test_clean_handoff_turn_is_checked_without_violations(make_harness: Callable[..., Harness]):
    harness = make_harness(WRONG_DETAIL_SCRIPTS)
    fourth = _play_wrong_details(harness, 4)
    assert harness.state.phase == "HUMAN_HANDOFF"
    assert fourth.debug["guard"] == {"checked": True, "violation": False, "regenerated": False, "fallback_used": False}
    assert not any(d.startswith("guard[") for d in fourth.debug["decisions"])


def test_grace_attempt_that_matches_stays_in_verify_id(make_harness: Callable[..., Harness]):
    harness = make_harness(WRONG_DETAIL_SCRIPTS)
    _play_wrong_details(harness, 3)
    assert harness.state.verify_attempts == 3

    fourth = harness.say("Sorry, my DOB 1985-03-15")
    state = harness.state
    assert state.phase == "VERIFY_ID"
    assert state.verified is False
    assert state.verify_attempts == 3  # a matching turn consumes no attempt
    assert state.verified_factors == {"name": True, "dob": True, "phone": False, "email": False, "id_last4": False}
    assert state.handoff_note is None
    directive = _directive(fourth)
    assert directive["phase"] == "VERIFY_ID"
    assert directive["facts"]["mismatch_this_turn"] is False
    assert directive["facts"]["matched_factor_count"] == 2
    assert directive["facts"]["factors_still_needed_count"] == 1
    assert directive["facts"]["offer_human"] is True  # still at max: the door stays open either way
    assert harness.state.frustration_streak == 0

    # And the door really is open: another mismatch after the grace match is attempt 4 and transfers.
    harness.say("My phone is 555-0000")
    assert harness.state.verify_attempts == 4
    assert harness.state.phase == "HUMAN_HANDOFF"
    assert IDENTITY_REASON in (harness.state.handoff_note or "")


# --------------------------------------------------------------------------------------------------
# Policyholder, account never located
# --------------------------------------------------------------------------------------------------


def test_not_located_path_offers_on_third_and_hands_off_on_fourth(make_harness: Callable[..., Harness]):
    harness = make_harness(NOT_FOUND_SCRIPTS)
    for turn in range(1, 4):
        result = harness.say("I'm Nobody Real, policy POL-0000")
        assert harness.state.phase == "VERIFY_ID", turn
        assert harness.state.verify_attempts == turn
    offer = _directive(result)
    assert offer["facts"]["offer_human"] is True and offer["facts"]["account_located"] is False
    assert offer["fallback_text"] == (
        FALLBACK_VERIFY_NOT_FOUND_PREFIX + FALLBACK_VERIFY_LOCATE.format(company_name=COMPANY_NAME) + FALLBACK_OFFER_HUMAN
    )
    assert offer["must_ask"].endswith("Offer to connect them with a human claims representative as an alternative.")

    fourth = harness.say("I'm Nobody Real, policy POL-0000")
    state = harness.state
    assert state.phase == "HUMAN_HANDOFF"
    assert state.verify_attempts == 4
    assert state.verified is False
    assert state.handoff_note is not None and IDENTITY_REASON in state.handoff_note
    _assert_handoff_directive(fourth, IDENTITY_REASON)
    assert state.slots.provided() == []  # locator slots cleared before the transfer, as on every mismatch
    assert state.public_state()["verify_attempts"] == 4


def test_turn_without_identity_details_consumes_no_attempt_at_max(make_harness: Callable[..., Harness]):
    harness = make_harness({**NOT_FOUND_SCRIPTS, "why": {}})
    for _ in range(3):
        harness.say("I'm Nobody Real, policy POL-0000")
    assert harness.state.verify_attempts == 3
    result = harness.say("Why do you need all this?")
    assert harness.state.phase == "VERIFY_ID"
    assert harness.state.verify_attempts == 3
    assert _directive(result)["facts"]["offer_human"] is True
    assert _directive(result)["facts"]["mismatch_this_turn"] is False


# --------------------------------------------------------------------------------------------------
# Threshold override
# --------------------------------------------------------------------------------------------------


def test_max_verify_attempts_one_offers_on_first_and_hands_off_on_second(make_harness: Callable[..., Harness]):
    harness = make_harness(WRONG_DETAIL_SCRIPTS, max_verify_attempts=1)
    first = harness.say(WRONG_DETAIL_TURNS[0])
    assert harness.state.phase == "VERIFY_ID"
    assert harness.state.verify_attempts == 1
    assert _directive(first)["facts"]["offer_human"] is True
    assert _directive(first)["phase"] == "VERIFY_ID"

    second = harness.say(WRONG_DETAIL_TURNS[1])
    assert harness.state.phase == "HUMAN_HANDOFF"
    assert harness.state.verify_attempts == 2
    assert harness.state.handoff_note is not None
    assert "identity not verified after 2 attempts" in harness.state.handoff_note
    _assert_handoff_directive(second, "identity not verified after 2 attempts")


# --------------------------------------------------------------------------------------------------
# Representative sub-flow
# --------------------------------------------------------------------------------------------------


def test_representative_third_unknown_pair_offers_and_fourth_hands_off(make_harness: Callable[..., Harness]):
    harness = make_harness(REP_SCRIPTS)
    for turn in range(1, 4):
        result = harness.say("Bob Jones calling for my aunt Margaret Chen")
        assert harness.state.phase == "VERIFY_ID", turn
        assert harness.state.caller_role == "representative"
        assert harness.state.verify_attempts == turn
        assert harness.state.consent_status is None
    third = _directive(result)
    assert third["phase"] == "VERIFY_ID"
    assert third["facts"]["offer_human"] is True
    assert third["facts"]["authorization_found"] is False
    assert third["fallback_text"] == FALLBACK_REPRESENTATIVE["mismatch"]
    assert harness.state.handoff_note is None

    fourth = harness.say("Bob Jones calling for my aunt Margaret Chen")
    state = harness.state
    assert state.phase == "HUMAN_HANDOFF"
    assert state.verified is False
    assert state.verify_attempts == 4
    assert state.consent_status is None
    assert state.handoff_note is not None
    assert REP_REASON in state.handoff_note
    assert "Caller: representative Bob Jones (nephew) for Margaret Chen; consent: not requested" in state.handoff_note
    _assert_handoff_directive(fourth, REP_REASON)
    assert not any(d.startswith(f"{DECISION_PREFIX}: no authorization") for d in fourth.debug["decisions"])
    assert "CL-" not in fourth.reply
    assert state.public_state()["verify_attempts"] == 4

    queued = harness.say("Hello?")
    assert queued.reply == HANDOFF_QUEUE_REPLY
    assert queued.debug is not None and queued.debug["llm_calls"] == []


def test_representative_turn_without_names_at_max_does_not_hand_off(make_harness: Callable[..., Harness]):
    harness = make_harness({**REP_SCRIPTS, "why": {}})
    for _ in range(3):
        harness.say("Bob Jones calling for my aunt Margaret Chen")
    assert harness.state.verify_attempts == 3
    harness.say("Why can't you find it?")
    assert harness.state.phase == "VERIFY_ID"
    assert harness.state.verify_attempts == 3


# --------------------------------------------------------------------------------------------------
# Refusal is not an attempt
# --------------------------------------------------------------------------------------------------


def test_refusing_twice_offers_a_human_but_never_hands_off_via_attempts(make_harness: Callable[..., Harness]):
    harness = make_harness(WRONG_DETAIL_SCRIPTS)
    first = harness.say("I refuse to give you any of that")
    assert _directive(first)["facts"]["offer_human"] is False
    second = harness.say("No. I refuse.")
    state = harness.state
    assert state.phase == "VERIFY_ID"
    assert state.verify_attempts == 0
    assert state.handoff_note is None
    assert _directive(second)["facts"]["offer_human"] is True
    assert _directive(second)["facts"]["mismatch_this_turn"] is False
    assert _directive(second)["phase"] == "VERIFY_ID"


def test_refusing_twice_at_max_attempts_still_only_offers(make_harness: Callable[..., Harness]):
    harness = make_harness(WRONG_DETAIL_SCRIPTS)
    _play_wrong_details(harness, 3)
    assert harness.state.verify_attempts == 3
    for _ in range(2):
        result = harness.say("I refuse to give you anything else")
        assert harness.state.phase == "VERIFY_ID"
        assert harness.state.verify_attempts == 3
        assert _directive(result)["facts"]["offer_human"] is True
    assert harness.state.handoff_note is None
    assert harness.state.frustration_streak == 2  # the frustration rule, not attempts, would be the next stop
