"""Immutable source records and the derived case objects.

Nothing in this module infers legal responsibility.  Every statement is an
assertion with a source, an author, a place, a time (or explicit unknown time),
and an epistemic mode.  Derived events keep a pointer back to the exact source
record and clause.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping, Sequence

from .lexicon import (
    CORRECTION_MARKERS,
    DEADLINE_START_ROLES,
    EventRole,
    MODE_LABELS,
    ROLE_LABELS,
    StatementMode,
    extract_clauses,
    find_markers,
    split_clauses,
)

UNASSIGNED = "UNASSIGNED"
SOURCE_KINDS = {
    "driver_note",
    "carrier_message",
    "recipient_email",
    "scan",
    "scan_correction",
    "manifest",
    "warehouse_note",
    "dock_note",
    "carrier_mapping",
}
AUTHOR_ROLES = {"driver", "carrier", "recipient", "warehouse", "broker", "system", "unknown"}

TIME_RE = re.compile(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b")
# At least one hyphenated segment; the greedy match keeps SHP-2026-0417-R whole
# rather than truncating to SHP-2026.
ID_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,7}(?:-[A-Z0-9]{1,8}){1,4}\b")
ZH_DATE_RE = re.compile(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日")
ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
LOCATION_RE = re.compile(
    r"(?:at|in|to)\s+([A-Z][A-Za-z0-9 .'-]{2,40}?)(?=[,.;]|\s+(?:not|and|but|at|on)\b|$)"
)
ZH_LOCATION_RE = re.compile(r"(?:在|放于|放於|到)([一-鿿A-Za-z0-9]{2,20}?(?:仓|倉|区|區|站|场|場|港|仓库|倉庫))")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def normalize_text(value: str) -> str:
    return " ".join(value.split())


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse an ISO-8601-ish timestamp, tolerating a trailing Z."""

    if not value:
        return None
    cleaned = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_timestamp(value: datetime | None, *, unknown: str = "时间未记录 / time not recorded") -> str:
    if value is None:
        return unknown
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


class Verification(str, Enum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    REJECTED = "rejected"


class EventStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ATTRIBUTION_WITHDRAWN = "attribution_withdrawn"
    UNASSIGNED = "unassigned"


class DeadlineStatus(str, Enum):
    UNKNOWN = "unknown"
    OPEN = "open"
    DUE_SOON = "due_soon"
    EXPIRED = "expired"


class ReviewState(str, Enum):
    NOT_REVIEWED = "not_reviewed"
    CURRENT = "current"
    STALE = "stale"


class ConfirmationStatus(str, Enum):
    OPEN = "open"
    EVIDENCE_AVAILABLE = "evidence_available"
    ANSWERED = "answered"
    WITHDRAWN = "withdrawn"


def infer_author_role(kind: str) -> str:
    return {
        "driver_note": "driver",
        "carrier_message": "carrier",
        "recipient_email": "recipient",
        "scan": "system",
        "scan_correction": "system",
        "manifest": "carrier",
        "warehouse_note": "warehouse",
        "dock_note": "warehouse",
        "carrier_mapping": "carrier",
    }.get(kind, "unknown")


def extract_calendar_date(text: str, reference: str | None) -> date | None:
    """Find an explicit date in the text; a missing year uses the reference year."""

    ref = parse_timestamp(reference)
    match = ZH_DATE_RE.search(text)
    if match:
        year = int(match.group(1)) if match.group(1) else (ref.year if ref else None)
        if year is None:
            return None
        try:
            return date(year, int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    match = ISO_DATE_RE.search(text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    return None


def extract_time_of_day(text: str) -> tuple[str | None, str]:
    """Return (HH:MM, precision).  Explicit metadata always wins later."""

    match = TIME_RE.search(text)
    if not match:
        return None, "none"
    return match.group(0), "time_only"


def combine_date_and_time(reference: str | None, time_of_day: str | None) -> str | None:
    """Attach a clock time to the date of the reference timestamp.

    The reference's UTC offset is preserved so a depot-local clock time is not
    silently reinterpreted as UTC.  The report still labels time-only evidence
    as provisional because the source's own timezone may differ.
    """

    if not time_of_day:
        return None
    ref = parse_timestamp(reference)
    if ref is None:
        return None
    original = datetime.fromisoformat(str(reference).replace("Z", "+00:00"))
    zone = original.tzinfo or timezone.utc
    hour, minute = (int(part) for part in time_of_day.split(":"))
    combined = ref.astimezone(zone).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return combined.isoformat()


def extract_locations(text: str) -> tuple[str, ...]:
    found: list[str] = []
    for match in LOCATION_RE.finditer(text):
        candidate = match.group(1).strip(" .'-")
        if candidate and candidate.lower() not in {"the", "a", "an"}:
            found.append(candidate)
    for match in ZH_LOCATION_RE.finditer(text):
        found.append(match.group(1).strip())
    seen: list[str] = []
    for item in found:
        if item not in seen:
            seen.append(item)
    return tuple(seen)


def extract_reference_ids(text: str) -> tuple[str, ...]:
    ids: list[str] = []
    for match in ID_RE.finditer(text):
        value = match.group(0).strip()
        if value not in ids:
            ids.append(value)
    return tuple(ids)


@dataclass(frozen=True)
class SourceRecord:
    """One immutable unit of source material."""

    source_id: str
    shipment_id: str
    kind: str
    lang: str
    text: str
    author: str
    author_role: str
    observed_at: str | None = None
    received_at: str | None = None
    location: str | None = None
    revision_of: str | None = None
    corrects: tuple[str, ...] = ()
    reassigns_to: str | None = None
    cross_references: tuple[str, ...] = ()
    note: str | None = None
    time_precision: str = "none"
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False)

    @property
    def is_correction(self) -> bool:
        if self.corrects or self.revision_of:
            return True
        return bool(find_markers(self.text, CORRECTION_MARKERS))

    @property
    def content_hash(self) -> str:
        return digest(
            {
                "source_id": self.source_id,
                "shipment_id": self.shipment_id,
                "kind": self.kind,
                "lang": self.lang,
                "text": normalize_text(self.text),
                "author": self.author,
                "author_role": self.author_role,
                "observed_at": self.observed_at,
                "received_at": self.received_at,
                "location": self.location,
                "revision_of": self.revision_of,
                "corrects": list(self.corrects),
                "reassigns_to": self.reassigns_to,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "shipment_id": self.shipment_id,
            "kind": self.kind,
            "lang": self.lang,
            "text": self.text,
            "author": self.author,
            "author_role": self.author_role,
            "observed_at": self.observed_at,
            "received_at": self.received_at,
            "location": self.location,
            "revision_of": self.revision_of,
            "corrects": list(self.corrects),
            "reassigns_to": self.reassigns_to,
            "cross_references": list(self.cross_references),
            "note": self.note,
            "time_precision": self.time_precision,
        }

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.source_id.strip():
            errors.append("source_id is required")
        if not self.shipment_id or self.shipment_id == UNASSIGNED:
            errors.append(f"{self.source_id}: shipment_id must be assigned explicitly")
        if self.kind not in SOURCE_KINDS:
            errors.append(f"{self.source_id}: unknown kind {self.kind!r}")
        if self.author_role not in AUTHOR_ROLES:
            errors.append(f"{self.source_id}: unknown author_role {self.author_role!r}")
        if not normalize_text(self.text):
            errors.append(f"{self.source_id}: text is empty")
        if self.observed_at and parse_timestamp(self.observed_at) is None:
            errors.append(f"{self.source_id}: observed_at is not a timestamp")
        if self.received_at and parse_timestamp(self.received_at) is None:
            errors.append(f"{self.source_id}: received_at is not a timestamp")
        if self.revision_of and self.revision_of == self.source_id:
            errors.append(f"{self.source_id}: cannot revise itself")
        if self.reassigns_to and not self.corrects:
            errors.append(f"{self.source_id}: reassigns_to requires corrects")
        return errors


def build_source(data: Mapping[str, Any], *, default_shipment: str | None = None) -> SourceRecord:
    """Build a source record from imported JSON, extracting only safe defaults."""

    text = str(data.get("text", ""))
    source_id = str(data.get("source_id") or data.get("id") or "").strip()
    kind = str(data.get("kind") or "unknown").strip()
    shipment_id = str(data.get("shipment_id") or default_shipment or "").strip()
    received_at = data.get("received_at")
    time_of_day, precision = extract_time_of_day(text)
    observed_at = data.get("observed_at")
    if not observed_at and time_of_day and received_at:
        explicit_date = extract_calendar_date(text, str(received_at))
        if explicit_date:
            zone = _timestamp_zone(str(received_at))
            hour, minute = (int(part) for part in time_of_day.split(":"))
            observed_at = datetime(
                explicit_date.year, explicit_date.month, explicit_date.day,
                hour, minute, tzinfo=zone,
            ).isoformat()
            precision = "date_from_text"
        else:
            observed_at = combine_date_and_time(str(received_at), time_of_day)
            precision = "time_only"
    elif observed_at:
        precision = str(data.get("time_precision") or "exact")
        observed_at = str(observed_at)
    location = data.get("location")
    if not location:
        locations = extract_locations(text)
        location = locations[0] if locations else None
    cross_references = data.get("cross_references")
    if cross_references is None:
        cross_references = extract_reference_ids(text)
    author = str(data.get("author") or _default_author(kind))
    return SourceRecord(
        source_id=source_id,
        shipment_id=shipment_id or UNASSIGNED,
        kind=kind,
        lang=str(data.get("lang") or "und"),
        text=text,
        author=author,
        author_role=str(data.get("author_role") or infer_author_role(kind)),
        observed_at=str(observed_at) if observed_at else None,
        received_at=str(received_at) if received_at else None,
        location=str(location) if location else None,
        revision_of=data.get("revision_of"),
        corrects=tuple(data.get("corrects") or ()),
        reassigns_to=data.get("reassigns_to"),
        cross_references=tuple(cross_references),
        note=data.get("note"),
        time_precision=precision if observed_at else "none",
        raw={key: value for key, value in data.items() if key not in {"text"}},
    )


def _default_author(kind: str) -> str:
    return {
        "driver_note": "driver (unnamed)",
        "carrier_message": "carrier (unnamed)",
        "recipient_email": "recipient (unnamed)",
        "scan": "carrier scan system",
        "scan_correction": "carrier scan system",
        "manifest": "carrier manifest",
        "warehouse_note": "warehouse (unnamed)",
        "dock_note": "dock (unnamed)",
    }.get(kind, "unknown author")


@dataclass(frozen=True)
class EvidenceEvent:
    """A clause-level assertion derived from one source record."""

    event_id: str
    source_id: str
    shipment_id: str
    clause_index: int
    text: str
    role: EventRole
    mode: StatementMode
    author: str
    author_role: str
    occurred_at: str | None
    time_precision: str
    location: str | None
    received_at: str | None
    status: EventStatus = EventStatus.ACTIVE
    superseded_by: tuple[str, ...] = ()
    attribution_note: str | None = None
    cross_references: tuple[str, ...] = ()

    @property
    def occurred(self) -> datetime | None:
        return parse_timestamp(self.occurred_at)

    @property
    def role_label(self) -> str:
        return ROLE_LABELS[self.role]

    @property
    def mode_label(self) -> str:
        return MODE_LABELS[self.mode]

    @property
    def is_speculation(self) -> bool:
        return self.mode is StatementMode.INFERRED

    @property
    def can_confirm_delivery(self) -> bool:
        """Only formal records or the performing party's own observation.

        A recipient's assertion, a relayed claim, or an inference never counts
        as confirmation.  This is a reporting guard, not a legal rule.
        """

        if self.role not in {EventRole.DELIVERY, EventRole.HANDOVER}:
            return False
        if self.mode is StatementMode.SYSTEM_RECORD:
            return True
        if self.mode is StatementMode.OBSERVED and self.author_role in {"driver", "carrier", "warehouse"}:
            return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "source_id": self.source_id,
            "shipment_id": self.shipment_id,
            "clause_index": self.clause_index,
            "text": self.text,
            "role": self.role.value,
            "mode": self.mode.value,
            "author": self.author,
            "author_role": self.author_role,
            "occurred_at": self.occurred_at,
            "time_precision": self.time_precision,
            "location": self.location,
            "received_at": self.received_at,
            "status": self.status.value,
            "superseded_by": list(self.superseded_by),
            "attribution_note": self.attribution_note,
            "cross_references": list(self.cross_references),
        }


def timestamp_key(value: str | None) -> float:
    """Sort key that compares absolute instants across differing UTC offsets.

    Timestamps are stored with their source offset (so a depot-local 18:40 is
    not rewritten as UTC), so naive string sorting would interleave +08:00 and
    Z records incorrectly.
    """

    parsed = parse_timestamp(value)
    return parsed.timestamp() if parsed else float("inf")


def clause_occurred_at(record: SourceRecord, clause: str) -> tuple[str | None, str]:
    """Per-clause time.  Explicit metadata wins; else a clock time in the clause.

    A clock time is turned into a timestamp only when a calendar date exists in
    the clause or the record metadata.  Otherwise the clause is recorded as
    time-unknown rather than fabricating a date.
    """

    clock, _ = extract_time_of_day(clause)
    if clock:
        declared = parse_timestamp(record.observed_at) if record.observed_at else None
        if declared is not None and record.time_precision in {"exact", "minute"}:
            return record.observed_at, "exact"
        explicit_date = extract_calendar_date(clause, record.received_at or record.observed_at)
        reference = record.received_at or record.observed_at
        if explicit_date and reference:
            zone = _timestamp_zone(reference)
            hour, minute = (int(part) for part in clock.split(":"))
            combined = datetime(
                explicit_date.year, explicit_date.month, explicit_date.day,
                hour, minute, tzinfo=zone,
            )
            return combined.isoformat(), "date_from_text"
        derived = combine_date_and_time(reference, clock)
        if derived:
            return derived, "time_only"
        return None, "time_only_unknown_date"
    # No clock time in this clause: only an explicitly declared record-level
    # observed_at may be inherited.  A clock time found elsewhere in the record
    # is not borrowed, because it may describe a different act in the sequence.
    if record.observed_at and record.time_precision == "exact":
        return record.observed_at, "record_metadata"
    return None, "none"


def _timestamp_zone(value: str) -> timezone:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.tzinfo or timezone.utc  # type: ignore[return-value]


def events_for_source(
    record: SourceRecord, *, shipment_id: str | None = None
) -> list[EvidenceEvent]:
    """Extract all clause events.  Unknown/boilerplate clauses are retained.

    Retention matters: a reviewer must be able to see that a line was read and
    classified as unrelated, rather than silently dropped.
    """

    events: list[EvidenceEvent] = []
    for index, clause in enumerate(extract_clauses(record.text, record.kind)):
        event_id = digest({"source_id": record.source_id, "clause_index": index})[:16]
        occurred_at, precision = clause_occurred_at(record, clause.text)
        locations = extract_locations(clause.text)
        events.append(
            EvidenceEvent(
                event_id=event_id,
                source_id=record.source_id,
                shipment_id=shipment_id or record.shipment_id,
                clause_index=index,
                text=clause.text,
                role=clause.role,
                mode=clause.mode,
                author=record.author,
                author_role=record.author_role,
                occurred_at=occurred_at,
                time_precision=precision,
                location=locations[0] if locations else record.location,
                received_at=record.received_at,
                cross_references=record.cross_references,
            )
        )
    return events


@dataclass(frozen=True)
class ShipmentLink:
    """A claimed old-id -> new-id correspondence.

    A link only takes effect when verified against a source that explicitly
    contains both identifiers and a renumbering/replacement statement.  This is
    intentionally stricter than string similarity.
    """

    link_id: str
    old_id: str
    new_id: str
    status: Verification
    evidence_ids: tuple[str, ...]
    rationale: str
    created_by: str
    created_at: str = field(default_factory=now_utc)

    def to_dict(self) -> dict[str, Any]:
        return {
            "link_id": self.link_id,
            "old_id": self.old_id,
            "new_id": self.new_id,
            "status": self.status.value,
            "evidence_ids": list(self.evidence_ids),
            "rationale": self.rationale,
            "created_by": self.created_by,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class DeadlinePolicy:
    policy_id: str = "default-review"
    version: str = "1"
    days_from_handover: int = 14
    due_soon_days: int = 3
    start_roles: tuple[EventRole, ...] = DEADLINE_START_ROLES
    note: str = (
        "内部复核用占位规则，不是法律时限意见；适用规则须由索赔人员确认。"
        " / Internal review placeholder, not a legal deadline opinion."
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "days_from_handover": self.days_from_handover,
            "due_soon_days": self.due_soon_days,
            "start_roles": [role.value for role in self.start_roles],
            "note": self.note,
        }


@dataclass(frozen=True)
class DeadlineCandidate:
    event_id: str
    source_id: str
    role: EventRole
    occurred_at: str
    basis: str
    usable: bool
    exclusion: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "source_id": self.source_id,
            "role": self.role.value,
            "occurred_at": self.occurred_at,
            "basis": self.basis,
            "usable": self.usable,
            "exclusion": self.exclusion,
        }


@dataclass(frozen=True)
class DeadlineAssessment:
    policy: DeadlinePolicy
    candidates: tuple[DeadlineCandidate, ...]
    chosen: DeadlineCandidate | None
    due_at: str | None
    status: DeadlineStatus
    input_digest: str
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy.to_dict(),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "chosen": self.chosen.to_dict() if self.chosen else None,
            "due_at": self.due_at,
            "status": self.status.value,
            "input_digest": self.input_digest,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class PendingConfirmation:
    question_id: str
    shipment_id: str
    question: str
    target_party: str
    status: ConfirmationStatus
    created_at: str
    created_by: str
    evidence_ids: tuple[str, ...] = ()
    answer: str | None = None
    answered_at: str | None = None
    answered_by: str | None = None
    answer_source_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "shipment_id": self.shipment_id,
            "question": self.question,
            "target_party": self.target_party,
            "status": self.status.value,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "evidence_ids": list(self.evidence_ids),
            "answer": self.answer,
            "answered_at": self.answered_at,
            "answered_by": self.answered_by,
            "answer_source_ids": list(self.answer_source_ids),
        }

    def with_evidence(self, evidence_ids: Iterable[str]) -> "PendingConfirmation":
        merged = tuple(dict.fromkeys((*self.evidence_ids, *evidence_ids)))
        if self.status is ConfirmationStatus.OPEN and merged != self.evidence_ids:
            return replace(self, evidence_ids=merged, status=ConfirmationStatus.EVIDENCE_AVAILABLE)
        return replace(self, evidence_ids=merged)


@dataclass(frozen=True)
class MaterialUse:
    source_id: str
    content_hash: str
    event_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "content_hash": self.content_hash,
            "event_ids": list(self.event_ids),
        }


@dataclass(frozen=True)
class SubmissionVersion:
    version: int
    kind: str  # original | supplement
    material: tuple[MaterialUse, ...]
    summary: str
    extractor_version: str
    digest: str
    created_at: str
    created_by: str
    note: str | None = None

    @staticmethod
    def _payload(
        *,
        version: int,
        kind: str,
        material: Sequence[MaterialUse],
        summary: str,
        extractor_version: str,
        created_at: str,
        created_by: str,
        note: str | None,
    ) -> dict[str, Any]:
        return {
            "version": version,
            "kind": kind,
            "material": [item.to_dict() for item in material],
            "summary": summary,
            "extractor_version": extractor_version,
            "created_at": created_at,
            "created_by": created_by,
            "note": note,
        }

    @staticmethod
    def create(
        *,
        version: int,
        kind: str,
        material: Sequence[MaterialUse],
        summary: str,
        extractor_version: str,
        created_at: str,
        created_by: str,
        note: str | None = None,
    ) -> "SubmissionVersion":
        payload = SubmissionVersion._payload(
            version=version,
            kind=kind,
            material=material,
            summary=summary,
            extractor_version=extractor_version,
            created_at=created_at,
            created_by=created_by,
            note=note,
        )
        return SubmissionVersion(
            version=version,
            kind=kind,
            material=tuple(material),
            summary=summary,
            extractor_version=extractor_version,
            digest=digest(payload),
            created_at=created_at,
            created_by=created_by,
            note=note,
        )

    def digest_is_valid(self) -> bool:
        payload = self._payload(
            version=self.version,
            kind=self.kind,
            material=self.material,
            summary=self.summary,
            extractor_version=self.extractor_version,
            created_at=self.created_at,
            created_by=self.created_by,
            note=self.note,
        )
        return digest(payload) == self.digest

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "kind": self.kind,
            "material": [item.to_dict() for item in self.material],
            "summary": self.summary,
            "extractor_version": self.extractor_version,
            "digest": self.digest,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "note": self.note,
        }


@dataclass(frozen=True)
class ClaimSubmission:
    """A submitted claim package.  Versions may be appended, never edited."""

    claim_id: str
    shipment_id: str
    carrier_ref: str | None
    versions: tuple[SubmissionVersion, ...]
    state: str = "submitted"

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "shipment_id": self.shipment_id,
            "carrier_ref": self.carrier_ref,
            "versions": [version.to_dict() for version in self.versions],
            "state": self.state,
        }

    @property
    def original(self) -> SubmissionVersion:
        return self.versions[0]

    @property
    def latest(self) -> SubmissionVersion:
        return self.versions[-1]

    def used_source_ids(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                item.source_id
                for version in self.versions
                for item in version.material
            )
        )

    def settle(self, version: SubmissionVersion) -> "ClaimSubmission":
        """Append a supplement only; the original version is never mutated."""

        if version.version != len(self.versions) + 1:
            raise ValueError("supplement version must be the next sequence number")
        if version.kind != "supplement":
            raise ValueError("appended versions must be supplements")
        return replace(self, versions=(*self.versions, version))

    def use_history(self) -> tuple[str, ...]:
        """Ordered source ids by version, e.g. v1: a,b; v2: c."""

        return tuple(
            f"v{version.version}: " + ", ".join(item.source_id for item in version.material)
            for version in self.versions
        )


@dataclass(frozen=True)
class ReviewCheckpoint:
    """An explicit review of deadline or draft liability position.

    The digest covers the decision inputs, so any new or corrected scan makes
    the checkpoint stale until a reviewer confirms it again.  ``basis`` is a
    short human-readable record of what the reviewer saw, which lets a later
    reader explain *why* a checkpoint went stale.
    """

    review_id: str
    shipment_id: str
    kind: str  # deadline | liability_draft
    digest: str
    as_of_source_count: int
    note: str
    actor: str
    basis: str = ""
    created_at: str = field(default_factory=now_utc)

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "shipment_id": self.shipment_id,
            "kind": self.kind,
            "digest": self.digest,
            "as_of_source_count": self.as_of_source_count,
            "note": self.note,
            "actor": self.actor,
            "basis": self.basis,
            "created_at": self.created_at,
        }


@dataclass
class CaseView:
    shipment_id: str
    canonical_id: str
    identity_warnings: tuple[str, ...]
    links: tuple[ShipmentLink, ...]
    sources: tuple[SourceRecord, ...]
    events: tuple[EvidenceEvent, ...]
    pending: tuple[PendingConfirmation, ...]
    submissions: tuple[ClaimSubmission, ...]
    deadline: DeadlineAssessment
    deadline_review: ReviewState
    deadline_review_note: str
    liability_review: ReviewState
    liability_review_note: str
    review_triggers: tuple[str, ...]
    orphan_notes: tuple[str, ...] = ()

    @property
    def title(self) -> str:
        if self.canonical_id != self.shipment_id:
            return f"{self.shipment_id} → {self.canonical_id}"
        return self.shipment_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "shipment_id": self.shipment_id,
            "canonical_id": self.canonical_id,
            "identity_warnings": list(self.identity_warnings),
            "links": [link.to_dict() for link in self.links],
            "sources": [source.to_dict() for source in self.sources],
            "events": [event.to_dict() for event in self.events],
            "pending": [item.to_dict() for item in self.pending],
            "submissions": [item.to_dict() for item in self.submissions],
            "deadline": self.deadline.to_dict(),
            "deadline_review": self.deadline_review.value,
            "deadline_review_note": self.deadline_review_note,
            "liability_review": self.liability_review.value,
            "liability_review_note": self.liability_review_note,
            "review_triggers": list(self.review_triggers),
            "orphan_notes": list(self.orphan_notes),
        }


def deadline_assessment(
    shipment_events: Sequence[EvidenceEvent],
    *,
    policy: DeadlinePolicy | None = None,
    today: date | None = None,
) -> DeadlineAssessment:
    """Assess review-deadline options from confirmed-ish clock-start events.

    Multiple starts are reported rather than collapsed, because a corrected
    scan can move the relevant handover.  Only active, time-bearing events that
    are not speculation and not withdrawn are usable.
    """

    policy = policy or DeadlinePolicy()
    today = today or datetime.now(timezone.utc).date()
    candidates: list[DeadlineCandidate] = []
    for event in sorted(
        shipment_events,
        key=lambda item: (timestamp_key(item.occurred_at), item.event_id),
    ):
        if event.role not in policy.start_roles:
            continue
        if event.occurred_at is None:
            candidates.append(
                DeadlineCandidate(
                    event_id=event.event_id,
                    source_id=event.source_id,
                    role=event.role,
                    occurred_at="",
                    basis="无可用时间 / no usable timestamp",
                    usable=False,
                    exclusion="缺少时间，不能作为时限起点",
                )
            )
            continue
        if event.status is not EventStatus.ACTIVE:
            candidates.append(
                DeadlineCandidate(
                    event_id=event.event_id,
                    source_id=event.source_id,
                    role=event.role,
                    occurred_at=event.occurred_at,
                    basis="已撤回或替代 / withdrawn or superseded",
                    usable=False,
                    exclusion=event.attribution_note or event.status.value,
                )
            )
            continue
        if event.is_speculation:
            candidates.append(
                DeadlineCandidate(
                    event_id=event.event_id,
                    source_id=event.source_id,
                    role=event.role,
                    occurred_at=event.occurred_at,
                    basis="推断，不作为起点 / inference only",
                    usable=False,
                    exclusion="来源为推断，需先核实",
                )
            )
            continue
        if event.mode is StatementMode.REPORTED:
            candidates.append(
                DeadlineCandidate(
                    event_id=event.event_id,
                    source_id=event.source_id,
                    role=event.role,
                    occurred_at=event.occurred_at,
                    basis="转述，需核实 / relayed",
                    usable=False,
                    exclusion="仅转述，不能作为时限起点",
                )
            )
            continue
        candidates.append(
            DeadlineCandidate(
                event_id=event.event_id,
                source_id=event.source_id,
                role=event.role,
                occurred_at=event.occurred_at,
                basis=f"{event.mode_label} / {event.author_role}",
                usable=True,
            )
        )

    usable = [candidate for candidate in candidates if candidate.usable]
    # Earliest usable clock start; formal scans are preferred when same minute.
    usable.sort(key=lambda item: (timestamp_key(item.occurred_at), item.role is not EventRole.SCAN))
    chosen = usable[0] if usable else None
    reasons: list[str] = []
    if chosen:
        reasons.append(
            f"采用最早可用事件 {chosen.event_id}（{ROLE_LABELS[chosen.role]}，{chosen.basis}）作为复核起点。"
        )
        reasons.append(policy.note)
        due = parse_timestamp(chosen.occurred_at)
        if due is None:  # defensive: usable candidates always carry timestamps
            return DeadlineAssessment(
                policy=policy,
                candidates=tuple(candidates),
                chosen=None,
                due_at=None,
                status=DeadlineStatus.UNKNOWN,
                input_digest=digest({"policy": policy.to_dict(), "candidates": candidates}),
                reasons=("起点时间无法解析，时限未定 / unparseable start",),
            )
        due = due + timedelta(days=policy.days_from_handover)
        days_left = (due.date() - today).days
        if days_left < 0:
            status = DeadlineStatus.EXPIRED
            reasons.append(f"按占位规则已逾期 {abs(days_left)} 天；须人工核对适用时限。")
        elif days_left <= policy.due_soon_days:
            status = DeadlineStatus.DUE_SOON
            reasons.append(f"距占位时限 {days_left} 天，需优先核对。")
        else:
            status = DeadlineStatus.OPEN
            reasons.append(f"距占位时限 {days_left} 天。")
        due_at = due.isoformat()
    else:
        reasons.append("没有可作为起点的可用交接/交付事件；时限未定，需人工判断。")
        due_at = None
        status = DeadlineStatus.UNKNOWN

    input_payload = {
        "policy": policy.to_dict(),
        "candidates": [candidate.to_dict() for candidate in candidates],
        "chosen": chosen.to_dict() if chosen else None,
    }
    return DeadlineAssessment(
        policy=policy,
        candidates=tuple(candidates),
        chosen=chosen,
        due_at=due_at,
        status=status,
        input_digest=digest(input_payload),
        reasons=tuple(reasons),
    )


def event_sort_key(event: EvidenceEvent) -> tuple[Any, ...]:
    occurred = event.occurred
    return (
        0 if occurred else 1,
        occurred.timestamp() if occurred else float("inf"),
        timestamp_key(event.received_at),
        event.source_id,
        event.clause_index,
    )

