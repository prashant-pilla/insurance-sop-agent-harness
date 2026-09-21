import shutil
from pathlib import Path

import pytest

from app.sop.state import CaseHints, IdentitySlots
from app.tools.data import (
    FixtureStore,
    normalize_date,
    normalize_name,
    normalize_phone,
    parse_time_hint,
)
from app.tools.email import mask_email
from tests.conftest import FIXTURES_DIR


def test_normalize_date_formats():
    for raw in ("March 15, 1985", "03/15/1985", "1985-03-15", "15 March 1985", "Mar 15 1985", "March 15th, 1985"):
        assert normalize_date(raw) == "1985-03-15", raw
    assert normalize_date("not a date") is None


def test_normalize_phone_strips_country_code():
    assert normalize_phone("+1 (650) 521-2836") == "6505212836"
    assert normalize_phone("650-521-2836") == "6505212836"
    assert normalize_phone("123") is None


def test_normalize_name_and_mask_email():
    assert normalize_name("  margaret   CHEN ") == "margaret chen"
    assert mask_email("margaret@email.com") == "m*****@email.com"


def test_parse_time_hint():
    assert parse_time_hint("January") == (1, None)
    assert parse_time_hint("Jan 2026") == (1, 2026)
    assert parse_time_hint("2025-11") == (11, 2025)
    assert parse_time_hint("recently") == (None, None)


def test_locate_and_verify_with_aliases(store: FixtureStore):
    slots = IdentitySlots(name="Yaven Li", phone="650-521-2830", email="YAWEN.LI@example.com")
    holder = store.locate_policyholder(slots)
    assert holder is not None and holder["party_id"] == "P13"
    result = store.verify_identity(holder, slots)
    assert result.count == 3
    assert result.matched["name"] and result.matched["phone"] and result.matched["email"]


def test_filter_claims_by_hints(store: FixtureStore):
    claims = store.list_claims("P9")
    matches, applied = store.filter_claims(claims, CaseHints(case_type="healthcare", status="denied", time_hint="January"))
    assert [c["case_id"] for c in matches] == ["CL-2048"]
    assert applied == ["case_type", "status", "time_hint"]
    matches, _ = store.filter_claims(claims, CaseHints(case_type="medical", time_hint="January"))
    assert {c["case_id"] for c in matches} == {"CL-2048", "CL-2011"}
    matches, applied = store.filter_claims(claims, CaseHints(time_hint="recently"))
    assert applied == [] and len(matches) == 4


def test_guidance_matching(store: FixtureStore):
    claim = store.get_claim("CL-2048")
    assert claim is not None
    facts = store.guidance_for_claim(claim, "denial_question", "how soon do I need to submit them?")
    assert "original pathology report" in facts["document_guidance"]
    assert "treating provider office note" in facts["document_guidance"]
    assert {"default", "original pathology report", "treating provider office note"} <= set(facts["document_alternative_guidance"])
    topics = {entry["topic"] for entry in facts["followup_guidance"]}
    assert "submission_timing" in topics
    timing = next(e for e in facts["followup_guidance"] if e["topic"] == "submission_timing")
    assert timing["text"] == "For claim CL-2048, please submit pathology report and office note within a week."
    assert facts["case_type_guidance"].startswith("For medical claims")


def test_consent_status_boundaries(store: FixtureStore):
    assert store.consent_status("default", 1) == "pending"
    assert store.consent_status("default", 2) == "approved"
    assert store.consent_status("default", 3) == "timed_out"
    assert store.consent_status("timeout", 5) == "pending"
    assert store.consent_status("timeout", 6) == "timed_out"
    with pytest.raises(ValueError):
        store.consent_status("bogus", 1)
    with pytest.raises(ValueError):
        store.consent_status("default", 0)


def test_consent_scenarios_lists_fixture_keys(store: FixtureStore):
    assert store.consent_scenarios() == ["default", "timeout"]
    assert len(store.representatives) == 1


def test_find_representative_matches_on_two_normalized_names(store: FixtureStore):
    record = store.find_representative("david chen", "MARGARET  CHEN")
    assert record is not None and record["buyer_party_id"] == "P9"
    assert record["relationship"] == "son"
    # Wrong representative, right policyholder.
    assert store.find_representative("Bob Jones", "Margaret Chen") is None
    # Right representative, wrong policyholder.
    assert store.find_representative("David Chen", "Yawen Li") is None
    # Missing either name never matches.
    assert store.find_representative(None, "Margaret Chen") is None
    assert store.find_representative("David Chen", "") is None


def test_find_representative_accepts_policyholder_alias(store: FixtureStore):
    holder = store.get_policyholder("P9")
    assert holder is not None
    for alias in holder.get("name_aliases", []):
        assert store.find_representative("David Chen", alias) is not None, alias


def test_store_tolerates_missing_representative_fixtures(tmp_path: Path):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    for name in ("policyholders.json", "claims.json", "claim_schema.json", "required_document_guideline.json"):
        shutil.copy(FIXTURES_DIR / name, fixtures / name)
    store = FixtureStore(fixtures)
    assert store.representatives == []
    assert store.find_representative("David Chen", "Margaret Chen") is None
    assert store.consent_scenarios() == ["default"]
    assert store.consent_status("default", 1) == "pending"
    assert store.consent_status("default", 2) == "approved"
    assert store.consent_status("default", 3) == "timed_out"
    with pytest.raises(ValueError):
        store.consent_status("timeout", 1)
