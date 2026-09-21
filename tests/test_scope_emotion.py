from app.sop.prompts import HANDOFF_QUEUE_REPLY, OFFER_HUMAN_INSTRUCTION
from tests.conftest import Harness


def test_out_of_scope_three_times_hands_off(make_harness):
    harness: Harness = make_harness({"what is rl": {"scope": "out_of_scope"}})
    first = harness.say("What is RL?")
    assert harness.state.off_topic_streak == 1
    assert harness.state.phase == "VERIFY_ID"
    assert first.debug is not None and first.debug["directive"]["facts"]["scope"] == "out_of_scope"

    second = harness.say("Come on, what is RL?")
    assert harness.state.off_topic_streak == 2
    assert second.debug is not None
    assert OFFER_HUMAN_INSTRUCTION in second.debug["directive"]["instructions"]

    harness.say("Seriously, what is RL?")
    assert harness.state.phase == "HUMAN_HANDOFF"
    assert harness.state.handoff_note is not None and "off-topic" in harness.state.handoff_note


def test_off_topic_streak_resets_on_in_scope_turn(make_harness):
    harness: Harness = make_harness({"what is rl": {"scope": "out_of_scope"}, "my name": {"name": "Margaret Chen"}})
    harness.say("What is RL?")
    harness.say("What is RL? Please")
    assert harness.state.off_topic_streak == 2
    harness.say("Fine. My name is Margaret Chen")
    assert harness.state.off_topic_streak == 0
    assert harness.state.phase == "VERIFY_ID"


def test_frustration_without_progress_hands_off(make_harness):
    harness: Harness = make_harness({"ridiculous": {"emotion": "angry"}})
    for turn in range(1, 3):
        result = harness.say("This is ridiculous, just tell me about my claim")
        assert harness.state.frustration_streak == turn
        assert harness.state.phase == "VERIFY_ID"
        assert result.debug is not None
        assert result.debug["directive"]["tone"].startswith("The caller is angry")
    harness.say("This is ridiculous!")
    assert harness.state.phase == "HUMAN_HANDOFF"
    assert harness.state.verified is False
    assert harness.state.handoff_note is not None and "Identity verified: no" in harness.state.handoff_note


def test_frustration_streak_resets_on_progress(make_harness):
    harness: Harness = make_harness(
        {"ridiculous": {"emotion": "angry"}, "fine": {"emotion": "frustrated", "name": "Margaret Chen"}}
    )
    harness.say("This is ridiculous")
    harness.say("This is ridiculous")
    assert harness.state.frustration_streak == 2
    harness.say("Fine, Margaret Chen")
    assert harness.state.frustration_streak == 0


def test_sustained_refusal_offers_human(make_harness):
    harness: Harness = make_harness({"not giving": {"emotion": "refusing", "name": "Margaret Chen"}})
    first = harness.say("I'm Margaret Chen and I'm not giving you my birthday")
    assert first.debug is not None and first.debug["directive"]["facts"]["offer_human"] is False
    second = harness.say("I said I'm not giving you that")
    assert second.debug is not None and second.debug["directive"]["facts"]["offer_human"] is True
    assert harness.state.phase == "VERIFY_ID"


def test_wants_human_hands_off_and_queue_reply_follows(make_harness):
    harness: Harness = make_harness({"real person": {"wants_human": True}})
    result = harness.say("Let me talk to a real person")
    assert harness.state.phase == "HUMAN_HANDOFF"
    assert harness.state.handoff_note is not None
    assert result.debug is not None and result.debug["directive"]["phase"] == "HUMAN_HANDOFF"

    follow_up = harness.say("Hello? Anyone there?")
    assert follow_up.reply == HANDOFF_QUEUE_REPLY
    assert follow_up.debug is not None and follow_up.debug["llm_calls"] == []


def test_in_scope_general_adds_labeled_guidance_and_human_offer(make_harness):
    harness: Harness = make_harness({"appeals generally": {"scope": "in_scope_general"}})
    result = harness.say("How do appeals generally work?")
    assert result.debug is not None
    instructions = result.debug["directive"]["instructions"]
    assert any("general information" in i for i in instructions)
    assert OFFER_HUMAN_INSTRUCTION in instructions
    assert harness.state.phase == "VERIFY_ID"
