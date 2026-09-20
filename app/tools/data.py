"""Fixture access: identity matching, claims lookup, guidance selection, guard terms."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.sop.state import FACTORS, CaseHints, IdentitySlots

logger = logging.getLogger(__name__)

DEFAULT_CONSENT_SCENARIOS: dict[str, Any] = {"default": {"status_sequence": ["pending", "approved"]}}

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%m/%d/%Y",
    "%m-%d-%Y",
    "%m.%d.%Y",
    "%B %d, %Y",
    "%B %d %Y",
    "%b %d, %Y",
    "%b %d %Y",
    "%d %B %Y",
    "%d %b %Y",
    "%d/%m/%Y",
)
_ORDINAL_RE = re.compile(r"(\d{1,2})(st|nd|rd|th)\b", re.IGNORECASE)

_MONTHS = {
    name: index
    for index, names in enumerate(
        (
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for name in names
}
_MONTH_RE = re.compile(r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(20\d{2}|19\d{2})\b")
_ISO_MONTH_RE = re.compile(r"\b(20\d{2})-(0[1-9]|1[0-2])\b")

_CASE_TYPE_KEYWORDS = {
    "healthcare": ("health", "medical", "hospital", "doctor", "surgery", "clinic"),
    "dental": ("dental", "dentist", "tooth", "teeth"),
    "auto": ("auto", "car", "vehicle", "collision", "accident"),
}
_STATUS_KEYWORDS = {
    "denied": ("denied", "denial", "rejected", "declined", "refused"),
    "open": ("open", "pending", "progress", "processing", "active", "review"),
    "closed": ("closed", "settled", "completed", "paid", "resolved", "finished"),
}
_CASE_ID_RE = re.compile(r"CL-\d+", re.IGNORECASE)


def normalize_date(value: str | None) -> str | None:
    if not value:
        return None
    text = _ORDINAL_RE.sub(r"\1", value.strip())
    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r"\s+", " ", text)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def normalize_phone(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) >= 7 else None


def normalize_email(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip().lower()
    return text if "@" in text else None


def normalize_name(value: str | None) -> str | None:
    if not value:
        return None
    text = " ".join(value.casefold().replace(".", " ").replace(",", " ").split())
    return text or None


def normalize_last4(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    return digits[-4:] if len(digits) >= 4 else None


def normalize_policy_number(value: str | None) -> str | None:
    if not value:
        return None
    text = re.sub(r"\s+", "", value).upper()
    return text or None


def canonical_case_type(value: str | None) -> str | None:
    if not value:
        return None
    text = value.lower()
    for canonical, keywords in _CASE_TYPE_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return canonical
    return None


def canonical_status(value: str | None) -> str | None:
    if not value:
        return None
    text = value.lower()
    for canonical, keywords in _STATUS_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return canonical
    return None


def parse_time_hint(value: str | None) -> tuple[int | None, int | None]:
    """Return (month, year) parsed from a free-text time hint; either may be None."""
    if not value:
        return None, None
    iso = _ISO_MONTH_RE.search(value)
    if iso:
        return int(iso.group(2)), int(iso.group(1))
    month_match = _MONTH_RE.search(value)
    year_match = _YEAR_RE.search(value)
    month = _MONTHS[month_match.group(1).lower()] if month_match else None
    year = int(year_match.group(1)) if year_match else None
    return month, year


@dataclass
class VerificationResult:
    matched: dict[str, bool] = field(default_factory=lambda: {f: False for f in FACTORS})
    mismatched: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return sum(1 for hit in self.matched.values() if hit)


def _holder_names(holder: dict[str, Any]) -> set[str]:
    values = [holder.get("name"), *holder.get("name_aliases", [])]
    return {normalize_name(v) for v in values if v}


def _holder_phones(holder: dict[str, Any]) -> set[str]:
    values = [holder.get("phone"), *holder.get("phone_aliases", [])]
    return {p for p in (normalize_phone(v) for v in values) if p}


def _holder_emails(holder: dict[str, Any]) -> set[str]:
    values = [holder.get("email"), *holder.get("email_aliases", [])]
    return {e for e in (normalize_email(v) for v in values) if e}


def _fuzzy_key_match(document: str, key: str) -> bool:
    doc_tokens = set(document.lower().split())
    key_tokens = set(key.lower().split())
    return doc_tokens <= key_tokens or key_tokens <= doc_tokens


def _load(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _load_optional(path: Path, default: Any) -> Any:
    """Load an optional fixture; a missing or unreadable file falls back to ``default`` with a warning."""
    try:
        return _load(path)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("optional fixture %s not loaded (%s); using default", path.name, exc)
        return json.loads(json.dumps(default))


class FixtureStore:
    def __init__(self, fixtures_dir: Path):
        self.policyholders: list[dict[str, Any]] = _load(fixtures_dir / "policyholders.json")
        self.claims: list[dict[str, Any]] = _load(fixtures_dir / "claims.json")
        self.claim_schema: dict[str, Any] = _load(fixtures_dir / "claim_schema.json")
        self.guidance: dict[str, Any] = _load(fixtures_dir / "required_document_guideline.json")
        self.representatives: list[dict[str, Any]] = _load_optional(fixtures_dir / "representatives.json", [])
        self.consent_scenarios_data: dict[str, Any] = _load_optional(
            fixtures_dir / "consent_scenarios.json", DEFAULT_CONSENT_SCENARIOS
        )

    def get_policyholder(self, party_id: str) -> dict[str, Any] | None:
        return next((p for p in self.policyholders if p["party_id"] == party_id), None)

    def find_representative(self, rep_name: str | None, policyholder_name: str | None) -> dict[str, Any] | None:
        """Match a representative record on normalized representative name and named policyholder.

        The policyholder is matched against the record's ``buyer_name`` and against the linked
        policyholder's ``name``/``name_aliases`` (via ``buyer_party_id``). Relationship is not matched.
        """
        rep = normalize_name(rep_name)
        holder_name = normalize_name(policyholder_name)
        if not rep or not holder_name:
            return None
        for record in self.representatives:
            if normalize_name(record.get("rep_name")) != rep:
                continue
            accepted = {normalize_name(record.get("buyer_name"))}
            holder = self.get_policyholder(record.get("buyer_party_id", ""))
            if holder:
                accepted |= _holder_names(holder)
            if holder_name in accepted:
                return record
        return None

    def consent_scenarios(self) -> list[str]:
        return list(self.consent_scenarios_data.keys())

    def consent_status(self, scenario: str, check_index: int) -> str:
        """Status at the 1-based ``check_index`` of the scenario; ``timed_out`` once the sequence is exhausted."""
        if scenario not in self.consent_scenarios_data:
            raise ValueError(f"unknown consent scenario '{scenario}'; valid: {', '.join(self.consent_scenarios())}")
        if check_index < 1:
            raise ValueError(f"check_index must be >= 1, got {check_index}")
        sequence: list[str] = self.consent_scenarios_data[scenario].get("status_sequence", [])
        if check_index > len(sequence):
            return "timed_out"
        return sequence[check_index - 1]

    def locate_policyholder(self, slots: IdentitySlots) -> dict[str, Any] | None:
        """Find the candidate record by policy number, then name, then phone, then email."""
        policy = normalize_policy_number(slots.policy_number)
        if policy:
            for holder in self.policyholders:
                if normalize_policy_number(holder["policy_number"]) == policy:
                    return holder
        name = normalize_name(slots.name)
        if name:
            for holder in self.policyholders:
                if name in _holder_names(holder):
                    return holder
        phone = normalize_phone(slots.phone)
        if phone:
            for holder in self.policyholders:
                if phone in _holder_phones(holder):
                    return holder
        email = normalize_email(slots.email)
        if email:
            for holder in self.policyholders:
                if email in _holder_emails(holder):
                    return holder
        return None

    def verify_identity(self, holder: dict[str, Any], slots: IdentitySlots) -> VerificationResult:
        """Compare provided factors against the record. Policy number is not a factor."""
        result = VerificationResult()
        checks = {
            "name": (normalize_name(slots.name), _holder_names(holder)),
            "dob": (normalize_date(slots.dob), {holder.get("dob")}),
            "phone": (normalize_phone(slots.phone), _holder_phones(holder)),
            "email": (normalize_email(slots.email), _holder_emails(holder)),
            "id_last4": (normalize_last4(slots.id_last4), {holder.get("id_last4")}),
        }
        for factor, (provided, expected) in checks.items():
            raw = getattr(slots, factor)
            if not raw:
                continue
            if provided is not None and provided in expected:
                result.matched[factor] = True
            else:
                result.mismatched.append(factor)
        return result

    def list_claims(self, party_id: str) -> list[dict[str, Any]]:
        return [c for c in self.claims if c["party_id"] == party_id]

    def get_claim(self, case_id: str) -> dict[str, Any] | None:
        return next((c for c in self.claims if c["case_id"].upper() == case_id.upper()), None)

    def filter_claims(
        self, claims: list[dict[str, Any]], hints: CaseHints
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Apply every usable hint. Returns (matching claims, names of hints applied)."""
        applied: list[str] = []
        matches = list(claims)
        case_id = _CASE_ID_RE.search(hints.case_id or "")
        if case_id:
            applied.append("case_id")
            matches = [c for c in matches if c["case_id"].upper() == case_id.group(0).upper()]
        case_type = canonical_case_type(hints.case_type)
        if case_type:
            applied.append("case_type")
            matches = [c for c in matches if c["case_type"] == case_type]
        status = canonical_status(hints.status)
        if status:
            applied.append("status")
            matches = [c for c in matches if c["status"] == status]
        month, year = parse_time_hint(hints.time_hint)
        if month or year:
            applied.append("time_hint")
            matches = [c for c in matches if _created_matches(c["created_at"], month, year)]
        return matches, applied

    def guidance_for_claim(self, claim: dict[str, Any], intent_path: str | None, message: str) -> dict[str, Any]:
        """Grounded facts for PROCESS_CASE: record, field meanings, and matching guidance."""
        guidance = self.guidance
        documents: list[str] = claim.get("documents_needed", [])
        doc_guidance = {
            key: entry["en"]
            for key, entry in guidance.get("document_guidance", {}).items()
            if any(_fuzzy_key_match(doc, key) for doc in documents)
        }
        alt_guidance = {
            key: entry["en"]
            for key, entry in guidance.get("document_alternative_guidance", {}).items()
            if key == "default" or any(_fuzzy_key_match(doc, key) for doc in documents)
        }
        settings = guidance.get("claim_followup_settings", {})
        avg_time = settings.get("average_processing_time_after_submission", {}).get("en", "")
        lowered = message.lower()
        followups = []
        for entry in guidance.get("claim_followup_guidance", []):
            if entry.get("requires_documents") and not documents:
                continue
            by_intent = intent_path is not None and intent_path in entry.get("intent_hints", [])
            by_phrase = any(phrase in lowered for phrase in entry.get("match_any", []))
            if by_intent or by_phrase:
                followups.append(
                    {
                        "topic": entry["topic"],
                        "text": entry["en"].format(
                            case_id=claim["case_id"],
                            documents=" and ".join(documents) or "the requested documents",
                            average_processing_time_after_submission=avg_time,
                        ),
                    }
                )
        return {
            "claim": claim,
            "field_descriptions": {
                key: value["description"]
                for key, value in self.claim_schema.get("field_descriptions", {}).items()
            },
            "document_guidance": doc_guidance,
            "document_alternative_guidance": alt_guidance,
            "case_type_guidance": guidance.get("case_type_guidance", {}).get(claim["case_type"], {}).get("en"),
            "default_guidance": guidance.get("default_guidance", {}).get("en"),
            "human_review_note": settings.get("human_review_after_document_alternatives_exhausted", {}).get("en"),
            "followup_guidance": followups,
            "followup_fallback": guidance.get("claim_followup_fallback", {}).get("en"),
        }

    def sensitive_terms(self) -> list[str]:
        """Literal strings from the claim fixtures that must never appear before verification."""
        terms: set[str] = set()
        for claim in self.claims:
            terms.add(claim["case_id"])
            for key in ("summary", "denial_reason", "appeal_deadline", "created_at"):
                if claim.get(key):
                    terms.add(str(claim[key]))
            terms.update(claim.get("documents_needed", []))
        return sorted(terms, key=len, reverse=True)

    def sensitive_amounts(self) -> list[str]:
        amounts: set[str] = set()
        for claim in self.claims:
            for key in ("expected_reimbursement_amount", "allowed_max_amount", "net_pay", "net_fee"):
                value = claim.get(key)
                if value and value != "0.00":
                    amounts.add(value.split(".")[0])
        return sorted(amounts)


def _created_matches(created_at: str, month: int | None, year: int | None) -> bool:
    created = datetime.strptime(created_at, "%Y-%m-%d").date()
    if month and created.month != month:
        return False
    if year and created.year != year:
        return False
    return True
