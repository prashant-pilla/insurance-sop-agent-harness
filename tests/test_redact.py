"""redact_pii / masked_extraction: what leaves the extractor must not carry raw identity values."""

import pytest

from app.sop.redact import masked_extraction, redact_pii
from app.sop.state import Extraction

DEMO_LINE = (
    "I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my denied "
    "healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472."
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Dates: ISO, US numeric, month-name with a day (with and without year, either order).
        ("DOB is 1985-03-15", "DOB is [DATE]"),
        ("born 1985/03/15", "born [DATE]"),
        ("DOB 03/15/1985", "DOB [DATE]"),
        ("DOB 3/15/1985", "DOB [DATE]"),
        ("DOB 15.03.1985", "DOB [DATE]"),
        ("born March 15th, 1985", "born [DATE]"),
        ("born March 15 1985", "born [DATE]"),
        ("born march 15, 1985", "born [DATE]"),
        ("born 15 March 1985", "born [DATE]"),
        ("surgery on Jan. 12, 2026", "surgery on [DATE]"),
        ("surgery on January 12", "surgery on [DATE]"),
        ("surgery on 2026-01-12", "surgery on [DATE]"),
        # Phones: dashes, dots, parens, +1, bare, 7-digit local.
        ("call 650-521-2836", "call [PHONE]"),
        ("call 650.521.2836", "call [PHONE]"),
        ("call (650) 521-2836", "call [PHONE]"),
        ("call +1 650 521 2836", "call [PHONE]"),
        ("call +16505212836", "call [PHONE]"),
        ("call 6505212836", "call [PHONE]"),
        ("call 555-0123", "call [PHONE]"),
        # Emails.
        ("it's margaret.chen@email.com", "it's [EMAIL]"),
        ("Margaret+claims@Example.CO.UK ok", "[EMAIL] ok"),
    ],
)
def test_always_on_patterns(text: str, expected: str):
    assert redact_pii(text, None) == expected


@pytest.mark.parametrize(
    "text",
    [
        "my name is Margaret Chen",
        "policy POL-9921",
        "claim CL-2048",
        "the claim was for 1450.00",
        "reimbursement of $1,450.00",
        "from January",
        "from January 2026",
        "from 2026-01",
        "in march",
        "last four is 4472",  # only redacted when the extraction confirms it is the id
        "order 1234567890123",  # 13 digits is not a phone shape
    ],
)
def test_case_hints_names_ids_and_amounts_survive(text: str):
    assert redact_pii(text, None) == text


def test_extraction_adds_exact_last4_and_dob():
    extraction = Extraction(dob="1985-03-15", id_last4="4472")
    out = redact_pii(DEMO_LINE, extraction)
    assert out == (
        "I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my denied "
        "healthcare claim from January. DOB is [DATE], SSN last four is [ID4]."
    )
    assert "1985" not in out and "4472" not in out


def test_last4_is_replaced_as_a_whole_word_only():
    extraction = Extraction(id_last4="4472")
    assert redact_pii("code 4472 and 44721 and 14472", extraction) == "code [ID4] and 44721 and 14472"


def test_exact_dob_value_is_replaced_even_in_an_unusual_format():
    # normalize_date does not know this shape, so the extractor keeps it raw; the literal still goes.
    extraction = Extraction(dob="15th of March 1985")
    assert redact_pii("born 15th of March 1985", extraction) == "born [DATE]"


def test_extraction_without_sensitive_values_is_a_no_op_beyond_patterns():
    extraction = Extraction(name="Margaret Chen", policy_number="POL-9921")
    text = "Margaret Chen, POL-9921, CL-2048, from January"
    assert redact_pii(text, extraction) == text


def test_empty_text():
    assert redact_pii("", None) == ""
    assert redact_pii("", Extraction(dob="1985-03-15")) == ""


def test_masked_extraction_preserves_keys_and_masks_only_present_values():
    extraction = Extraction(
        name="Margaret Chen",
        policy_number="POL-9921",
        dob="1985-03-15",
        id_last4="4472",
        intent_hints=["denied claim"],
    )
    masked = masked_extraction(extraction)
    assert set(masked) == set(Extraction.model_fields)
    assert masked["dob"] == "[redacted]"
    assert masked["id_last4"] == "[redacted]"
    assert masked["phone"] is None
    assert masked["email"] is None
    assert masked["name"] == "Margaret Chen"
    assert masked["policy_number"] == "POL-9921"
    assert masked["intent_hints"] == ["denied claim"]
    assert "1985" not in str(masked) and "4472" not in str(masked)


def test_masked_extraction_of_empty_extraction_equals_model_dump():
    extraction = Extraction()
    assert masked_extraction(extraction) == extraction.model_dump()
