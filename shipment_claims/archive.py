"""Append-only archive with lineage, verified ID links, and review state.

Design constraints enforced here:

* a source record is written once; "editing" means appending a revision
* old/new shipment identifiers merge only through a verified, evidenced link
* submitted claim versions are never rewritten; supplements append
* deadline and draft-liability checkpoints become stale on new/corrected scans
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import EXTRACTOR_VERSION
from .models import (
    CaseView,
    ClaimSubmission,
    ConfirmationStatus,
    DeadlinePolicy,
    EvidenceEvent,
    EventStatus,
    MaterialUse,
    PendingConfirmation,
    ReviewCheckpoint,
    ReviewState,
    ShipmentLink,
    SourceRecord,
    SubmissionVersion,
    Verification,
    build_source,
    deadline_assessment,
    digest,
    event_sort_key,
    events_for_source,
    now_utc,
    timestamp_key,
)
from .lexicon import EventRole


class ArchiveError(ValueError):
    """Raised when an operation would violate archive integrity."""


class IdMergeRefused(ArchiveError):
    """Raised when identifiers are not backed by a verifiable link."""


def _stable_id(prefix: str, payload: Any, length: int = 12) -> str:
    return f"{prefix}-{digest(payload)[:length]}"


class ClaimArchive:
    """In-memory archive that serialises to a JSON document."""

    SCHEMA = "shipment-claims-archive/1"

    def __init__(
        self,
        *,
        policy: DeadlinePolicy | None = None,
        extractor_version: str = EXTRACTOR_VERSION,
    ) -> None:
        self.policy = policy or DeadlinePolicy()
        self.extractor_version = extractor_version
        self._sources: dict[str, SourceRecord] = {}
        self._events: dict[str, EvidenceEvent] = {}
        self._links: dict[str, ShipmentLink] = {}
        self._pending: dict[str, PendingConfirmation] = {}
        self._submissions: dict[str, ClaimSubmission] = {}
        self._checkpoints: list[ReviewCheckpoint] = []
        self._warnings: list[str] = []

    # ------------------------------------------------------------------ load
    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "ClaimArchive":
        if document.get("schema") != cls.SCHEMA:
            raise ArchiveError(f"unsupported archive schema: {document.get('schema')!r}")
        policy_data = document.get("deadline_policy") or {}
        policy = DeadlinePolicy(
            policy_id=policy_data.get("policy_id", "default-review"),
            version=str(policy_data.get("version", "1")),
            days_from_handover=int(policy_data.get("days_from_handover", 14)),
            due_soon_days=int(policy_data.get("due_soon_days", 3)),
            start_roles=tuple(
                EventRole(value) for value in policy_data.get("start_roles", [])
            ) or DeadlinePolicy().start_roles,
            note=policy_data.get("note", DeadlinePolicy().note),
        )
        archive = cls(policy=policy, extractor_version=str(document.get("extractor_version", EXTRACTOR_VERSION)))
        for source_data in document.get("sources", []):
            archive.add_source(build_source(source_data), record_event=False)
        for link_data in document.get("links", []):
            link = ShipmentLink(
                link_id=link_data["link_id"],
                old_id=link_data["old_id"],
                new_id=link_data["new_id"],
                status=Verification(link_data["status"]),
                evidence_ids=tuple(link_data.get("evidence_ids", ())),
                rationale=link_data.get("rationale", ""),
                created_by=link_data.get("created_by", "import"),
                created_at=link_data.get("created_at", now_utc()),
            )
            archive._links[link.link_id] = link
        for pending_data in document.get("pending", []):
            pending = PendingConfirmation(
                question_id=pending_data["question_id"],
                shipment_id=pending_data["shipment_id"],
                question=pending_data["question"],
                target_party=pending_data["target_party"],
                status=ConfirmationStatus(pending_data["status"]),
                created_at=pending_data["created_at"],
                created_by=pending_data["created_by"],
                evidence_ids=tuple(pending_data.get("evidence_ids", ())),
                answer=pending_data.get("answer"),
                answered_at=pending_data.get("answered_at"),
                answered_by=pending_data.get("answered_by"),
                answer_source_ids=tuple(pending_data.get("answer_source_ids", ())),
            )
            archive._pending[pending.question_id] = pending
        for claim_data in document.get("submissions", []):
            versions = tuple(
                SubmissionVersion(
                    version=version["version"],
                    kind=version["kind"],
                    material=tuple(
                        MaterialUse(
                            source_id=item["source_id"],
                            content_hash=item["content_hash"],
                            event_ids=tuple(item.get("event_ids", ())),
                        )
                        for item in version.get("material", ())
                    ),
                    summary=version["summary"],
                    extractor_version=version.get("extractor_version", ""),
                    digest=version["digest"],
                    created_at=version["created_at"],
                    created_by=version["created_by"],
                    note=version.get("note"),
                )
                for version in claim_data.get("versions", ())
            )
            archive._submissions[claim_data["claim_id"]] = ClaimSubmission(
                claim_id=claim_data["claim_id"],
                shipment_id=claim_data["shipment_id"],
                carrier_ref=claim_data.get("carrier_ref"),
                versions=versions,
                state=claim_data.get("state", "submitted"),
            )
        for checkpoint_data in document.get("checkpoints", []):
            archive._checkpoints.append(
                ReviewCheckpoint(
                    review_id=checkpoint_data["review_id"],
                    shipment_id=checkpoint_data["shipment_id"],
                    kind=checkpoint_data["kind"],
                    digest=checkpoint_data["digest"],
                    as_of_source_count=int(checkpoint_data["as_of_source_count"]),
                    note=checkpoint_data.get("note", ""),
                    actor=checkpoint_data.get("actor", "import"),
                    basis=checkpoint_data.get("basis", ""),
                    created_at=checkpoint_data.get("created_at", now_utc()),
                )
            )
        archive.rebuild_events()
        archive._warnings = list(document.get("warnings", ()))
        return archive

    @classmethod
    def from_export(cls, path: str | Path) -> "ClaimArchive":
        return cls.from_document(json.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def from_source_list(
        cls,
        records: Iterable[Mapping[str, Any]],
        *,
        default_shipment: str | None = None,
        policy: DeadlinePolicy | None = None,
    ) -> "ClaimArchive":
        archive = cls(policy=policy)
        for item in records:
            archive.add_source(build_source(item, default_shipment=default_shipment))
        return archive

    # ------------------------------------------------------------------ save
    def to_document(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "extractor_version": self.extractor_version,
            "exported_at": now_utc(),
            "deadline_policy": self.policy.to_dict(),
            "sources": [source.to_dict() for source in self._sources.values()],
            "links": [link.to_dict() for link in self._links.values()],
            "pending": [item.to_dict() for item in self._pending.values()],
            "submissions": [claim.to_dict() for claim in self._submissions.values()],
            "checkpoints": [checkpoint.to_dict() for checkpoint in self._checkpoints],
            "warnings": list(self._warnings),
        }

    def save(self, path: str | Path) -> None:
        """Write atomically so an interrupted save cannot truncate the archive."""

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.to_document(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)

    # --------------------------------------------------------------- sources
    @property
    def sources(self) -> tuple[SourceRecord, ...]:
        return tuple(self._sources.values())

    def add_source(self, record: SourceRecord, *, record_event: bool = True) -> SourceRecord:
        errors = record.validate()
        if errors:
            raise ArchiveError("; ".join(errors))
        existing = self._sources.get(record.source_id)
        if existing is not None:
            if existing.content_hash == record.content_hash:
                return existing
            raise ArchiveError(
                f"source {record.source_id!r} already archived with different content; "
                "append a revision instead of rewriting it"
            )
        self._sources[record.source_id] = record
        if record_event:
            for event in events_for_source(record):
                self._events[event.event_id] = event
        # Applying correction semantics immediately keeps an incremental import
        # consistent with a full rebuild.
        self._apply_corrections()
        self._apply_reassignments()
        self._reconcile_pending()
        return record

    def source(self, source_id: str) -> SourceRecord:
        try:
            return self._sources[source_id]
        except KeyError as exc:
            raise ArchiveError(f"unknown source {source_id!r}") from exc

    def rebuild_events(self) -> None:
        """Recompute extraction and re-apply corrections/links deterministically."""

        self._events = {}
        for record in self._sources.values():
            for event in events_for_source(record):
                self._events[event.event_id] = event
        self._warnings = [w for w in self._warnings if "corrects" not in w and "revises" not in w]
        for record in self._sources.values():
            for corrected in record.corrects:
                if corrected not in self._sources:
                    self._warnings.append(
                        f"source {record.source_id} corrects {corrected}, which is not in this archive"
                    )
            if record.revision_of and record.revision_of not in self._sources:
                self._warnings.append(
                    f"source {record.source_id} revises {record.revision_of}, which is not in this archive"
                )
        self._apply_corrections()
        self._apply_reassignments()
        self._reconcile_pending()

    def _apply_corrections(self) -> None:
        """Mark corrected underlying events as superseded, never delete them."""

        for record in self._sources.values():
            for target_id in record.corrects:
                target = self._sources.get(target_id)
                if target is None:
                    continue
                for event in list(self._events.values()):
                    if event.source_id != target_id:
                        continue
                    superseded = replace(
                        event,
                        status=EventStatus.SUPERSEDED,
                        superseded_by=tuple(
                            sorted({*event.superseded_by, record.source_id})
                        ),
                        attribution_note=(
                            f"{event.attribution_note + '; ' if event.attribution_note else ''}"
                            f"由 {record.source_id} 更正 / superseded by correction"
                        ),
                    )
                    self._events[event.event_id] = superseded

    def _apply_reassignments(self) -> None:
        """Withdraw attribution where a correction reassigns an event elsewhere.

        Two cases are handled:

        * events from the *corrected* source are withdrawn from this shipment
        * a clause inside the correction itself that names the other parcel is
          withdrawn, so it does not re-enter this shipment's timeline
        """

        for record in self._sources.values():
            if not (record.reassigns_to and record.corrects):
                continue
            reassigned = _normalise_id(record.reassigns_to)
            for target_id in record.corrects:
                for event in list(self._events.values()):
                    if event.source_id != target_id:
                        continue
                    self._withdraw_event(
                        event,
                        f"{record.source_id} 表明该事件属于另一货件 "
                        f"{record.reassigns_to} / attribution withdrawn",
                    )
            for event in list(self._events.values()):
                if event.source_id != record.source_id:
                    continue
                if reassigned and reassigned in _normalise_id(event.text):
                    self._withdraw_event(
                        event,
                        f"本段描述的是另一货件 {record.reassigns_to}，"
                        "不属于本货件 / clause belongs to another shipment",
                    )

    def _withdraw_event(self, event: EvidenceEvent, note: str) -> None:
        self._events[event.event_id] = replace(
            event,
            status=EventStatus.ATTRIBUTION_WITHDRAWN,
            attribution_note=note,
        )

    # --------------------------------------------------------------- identity
    def proposed_link(
        self,
        old_id: str,
        new_id: str,
        *,
        evidence_ids: Sequence[str],
        rationale: str,
        created_by: str,
    ) -> ShipmentLink:
        """Create a proposed (unverified) link.  It does not merge shipments."""

        if old_id == new_id:
            raise ArchiveError("old and new identifier are identical")
        evidence_ids = tuple(dict.fromkeys(evidence_ids))
        if not evidence_ids:
            raise IdMergeRefused(
                "carrier renumbering needs at least one evidence source naming both identifiers"
            )
        for evidence_id in evidence_ids:
            if evidence_id not in self._sources:
                raise IdMergeRefused(f"link evidence {evidence_id!r} is not archived")
        link = ShipmentLink(
            link_id=_stable_id("link", {"old": old_id, "new": new_id, "evidence": evidence_ids}),
            old_id=old_id,
            new_id=new_id,
            status=Verification.UNVERIFIED,
            evidence_ids=evidence_ids,
            rationale=rationale,
            created_by=created_by,
        )
        existing = self._links.get(link.link_id)
        if existing is not None:
            if existing.status is Verification.REJECTED:
                raise IdMergeRefused(
                    f"link {link.link_id} was rejected; a rejected identity merge is not "
                    "resurrected by re-proposing the same identifiers"
                )
            # Keep a verified link verified when the same proposal is re-imported.
            return existing
        self._links[link.link_id] = link
        return link

    def verify_link(self, link_id: str, *, verified_by: str, note: str = "") -> ShipmentLink:
        """Verify an existing proposed link after human review."""

        try:
            link = self._links[link_id]
        except KeyError as exc:
            raise ArchiveError(f"unknown link {link_id!r}") from exc
        if link.status is Verification.REJECTED:
            raise ArchiveError(f"link {link_id!r} was rejected and cannot be verified")
        self._require_link_evidence(link)
        verified = replace(
            link,
            status=Verification.VERIFIED,
            rationale=(link.rationale + (" | " if note else "") + note).strip(" |"),
            created_by=verified_by,
        )
        self._links[link_id] = verified
        return verified

    def reject_link(self, link_id: str, *, rejected_by: str, reason: str) -> ShipmentLink:
        try:
            link = self._links[link_id]
        except KeyError as exc:
            raise ArchiveError(f"unknown link {link_id!r}") from exc
        if not reason.strip():
            raise ArchiveError("a rejection reason is required")
        rejected = replace(
            link,
            status=Verification.REJECTED,
            rationale=f"{link.rationale} | 拒绝: {reason} ({rejected_by})".strip(" |"),
        )
        self._links[link_id] = rejected
        return rejected

    def _require_link_evidence(self, link: ShipmentLink) -> None:
        old_norm = _normalise_id(link.old_id)
        new_norm = _normalise_id(link.new_id)
        for evidence_id in link.evidence_ids:
            record = self._sources.get(evidence_id)
            if record is None:
                raise IdMergeRefused(f"link evidence {evidence_id!r} is missing")
            text_norm = _normalise_id(record.text)
            if old_norm in text_norm and new_norm in text_norm:
                return
        raise IdMergeRefused(
            "no single linked source names both the old and new identifier; "
            "string similarity is not sufficient"
        )

    def canonical_map(self) -> dict[str, str]:
        """Resolve verified links into old->new pointers, refusing cycles/conflicts."""

        parent: dict[str, str] = {}
        for link in self._links.values():
            if link.status is not Verification.VERIFIED:
                continue
            if link.old_id in parent and parent[link.old_id] != link.new_id:
                raise IdMergeRefused(
                    f"identifier {link.old_id!r} verified to both {parent[link.old_id]!r} "
                    f"and {link.new_id!r}; resolve manually"
                )
            parent[link.old_id] = link.new_id
        for start in parent:
            seen = {start}
            current = start
            while current in parent:
                current = parent[current]
                if current in seen:
                    raise IdMergeRefused(
                        f"verified links form a cycle involving {current!r}; resolve manually"
                    )
                seen.add(current)
        return parent

    def resolve_id(self, shipment_id: str) -> str:
        parent = self.canonical_map()
        seen: set[str] = {shipment_id}
        current = shipment_id
        while current in parent:
            current = parent[current]
            if current in seen:
                raise IdMergeRefused(f"verified links form a cycle at {current!r}")
            seen.add(current)
        return current

    def identity_warnings(self) -> list[str]:
        warnings: list[str] = []
        parent: dict[str, str] = {}
        for link in self._links.values():
            if link.status is not Verification.VERIFIED:
                continue
            if link.old_id in parent and parent[link.old_id] != link.new_id:
                warnings.append(
                    f"{link.old_id} 被核验指向多个新号 ({parent[link.old_id]}, {link.new_id})"
                )
            parent[link.old_id] = link.new_id
        # Cycles would make canonical resolution ambiguous.
        for start in list(parent):
            seen = {start}
            current = start
            while current in parent:
                current = parent[current]
                if current in seen:
                    warnings.append(f"新号对应关系存在环: {' -> '.join(sorted(seen))}")
                    break
                seen.add(current)
        return warnings

    def merge_shipment(self, old_id: str, new_id: str, *, link_id: str) -> None:
        """Explicitly acknowledge a verified merge.  Never merges on similarity."""

        link = self._links.get(link_id)
        if link is None:
            raise IdMergeRefused(f"unknown link {link_id!r}")
        if link.status is not Verification.VERIFIED:
            raise IdMergeRefused("link is not verified")
        if (link.old_id, link.new_id) != (old_id, new_id):
            raise IdMergeRefused("link does not describe this identifier pair")
        self._require_link_evidence(link)

    def similarity_warnings(self) -> list[str]:
        """Flag lookalike identifiers that are *not* linked, for human review."""

        warnings: list[str] = []
        ids = sorted(self._sources)
        shipment_ids = sorted({record.shipment_id for record in self._sources.values()})
        for index, left in enumerate(shipment_ids):
            for right in shipment_ids[index + 1:]:
                if self._linked(left, right):
                    continue
                if _lookalike(left, right):
                    warnings.append(
                        f"{left} 与 {right} 字符串相似但无核验对应关系，未合并 / "
                        "lookalike identifiers left separate"
                    )
        del ids
        return warnings

    def _linked(self, left: str, right: str) -> bool:
        for link in self._links.values():
            if link.status is Verification.VERIFIED and {link.old_id, link.new_id} == {left, right}:
                return True
        return False

    # -------------------------------------------------------- pending review
    def add_pending(
        self,
        *,
        shipment_id: str,
        question: str,
        target_party: str,
        created_by: str,
        evidence_ids: Sequence[str] = (),
    ) -> PendingConfirmation:
        question_id = _stable_id(
            "q", {"shipment": shipment_id, "question": question, "created_by": created_by}
        )
        if question_id in self._pending:
            return self._pending[question_id]
        for evidence_id in evidence_ids:
            if evidence_id not in self._sources:
                raise ArchiveError(f"question evidence {evidence_id!r} is not archived")
        pending = PendingConfirmation(
            question_id=question_id,
            shipment_id=shipment_id,
            question=question,
            target_party=target_party,
            status=ConfirmationStatus.OPEN,
            created_at=now_utc(),
            created_by=created_by,
            evidence_ids=tuple(evidence_ids),
        )
        self._pending[question_id] = pending
        return pending

    def answer_pending(
        self,
        question_id: str,
        *,
        answer: str,
        answered_by: str,
        source_ids: Sequence[str] = (),
    ) -> PendingConfirmation:
        try:
            pending = self._pending[question_id]
        except KeyError as exc:
            raise ArchiveError(f"unknown question {question_id!r}") from exc
        if pending.status is ConfirmationStatus.WITHDRAWN:
            raise ArchiveError("withdrawn questions cannot be answered")
        for source_id in source_ids:
            if source_id not in self._sources:
                raise ArchiveError(f"answer source {source_id!r} is not archived")
        updated = replace(
            pending,
            status=ConfirmationStatus.ANSWERED,
            answer=answer,
            answered_at=now_utc(),
            answered_by=answered_by,
            answer_source_ids=tuple(dict.fromkeys(source_ids)),
        )
        self._pending[question_id] = updated
        return updated

    def withdraw_pending(self, question_id: str, *, reason: str, actor: str) -> PendingConfirmation:
        try:
            pending = self._pending[question_id]
        except KeyError as exc:
            raise ArchiveError(f"unknown question {question_id!r}") from exc
        updated = replace(
            pending,
            status=ConfirmationStatus.WITHDRAWN,
            answer=f"撤回 ({actor}): {reason}",
            answered_at=now_utc(),
            answered_by=actor,
        )
        self._pending[question_id] = updated
        return updated

    def _reconcile_pending(self) -> None:
        """Link sources that arrive after a question to it, for reviewer context."""

        for question_id, pending in list(self._pending.items()):
            if pending.status is not ConfirmationStatus.OPEN:
                continue
            try:
                canonical = self.resolve_id(pending.shipment_id)
            except IdMergeRefused:
                # An ambiguous identity must not break archiving; the conflict
                # is surfaced by identity_warnings() instead.
                continue
            evidence: list[str] = []
            for record in self._sources.values():
                try:
                    same_shipment = self.resolve_id(record.shipment_id) == canonical
                except IdMergeRefused:
                    continue
                if not same_shipment:
                    continue
                if record.received_at and pending.created_at:
                    # A same-day source cannot be ordered against a wall-clock
                    # question time, so it still counts; only clearly earlier
                    # material is excluded.
                    if timestamp_key(record.received_at) < timestamp_key(pending.created_at) - 86400:
                        continue
                if any(term in record.text.lower() for term in _question_terms(pending.question)):
                    evidence.append(record.source_id)
            if evidence:
                self._pending[question_id] = pending.with_evidence(evidence)

    def pending_for(self, shipment_id: str) -> list[PendingConfirmation]:
        canonical = self.resolve_id(shipment_id)
        return sorted(
            (item for item in self._pending.values() if self.resolve_id(item.shipment_id) == canonical),
            key=lambda item: (item.status.value, item.created_at),
        )

    # ------------------------------------------------------------ submissions
    def submit_claim(
        self,
        *,
        claim_id: str,
        shipment_id: str,
        summary: str,
        created_by: str,
        source_ids: Sequence[str] | None = None,
        carrier_ref: str | None = None,
        note: str | None = None,
    ) -> ClaimSubmission:
        if claim_id in self._submissions:
            raise ArchiveError(
                f"claim {claim_id!r} is already submitted; append a supplement"
            )
        material = self._material_for(shipment_id, source_ids)
        if not material:
            raise ArchiveError("a claim needs at least one material source")
        version = SubmissionVersion.create(
            version=1,
            kind="original",
            material=material,
            summary=summary,
            extractor_version=self.extractor_version,
            created_at=now_utc(),
            created_by=created_by,
            note=note,
        )
        claim = ClaimSubmission(
            claim_id=claim_id,
            shipment_id=shipment_id,
            carrier_ref=carrier_ref,
            versions=(version,),
        )
        self._submissions[claim_id] = claim
        return claim

    def supplement_claim(
        self,
        claim_id: str,
        *,
        summary: str,
        created_by: str,
        source_ids: Sequence[str] | None = None,
        note: str | None = None,
    ) -> ClaimSubmission:
        try:
            claim = self._submissions[claim_id]
        except KeyError as exc:
            raise ArchiveError(f"unknown claim {claim_id!r}") from exc
        material = self._material_for(claim.shipment_id, source_ids)
        if not material:
            raise ArchiveError("a supplement needs at least one material source")
        already = {item.source_id for version in claim.versions for item in version.material}
        new_only = tuple(item for item in material if item.source_id not in already)
        if not new_only:
            raise ArchiveError("supplement contains no source not already used")
        version = SubmissionVersion.create(
            version=len(claim.versions) + 1,
            kind="supplement",
            material=new_only,
            summary=summary,
            extractor_version=self.extractor_version,
            created_at=now_utc(),
            created_by=created_by,
            note=note,
        )
        updated = claim.settle(version)
        self._submissions[claim_id] = updated
        return updated

    def _material_for(
        self, shipment_id: str, source_ids: Sequence[str] | None
    ) -> tuple[MaterialUse, ...]:
        canonical = self.resolve_id(shipment_id)
        chosen: list[SourceRecord] = []
        if source_ids is None:
            chosen = sorted(
                (record for record in self._sources.values()
                 if self.resolve_id(record.shipment_id) == canonical),
                key=lambda record: (timestamp_key(record.received_at), record.source_id),
            )
        else:
            for source_id in source_ids:
                record = self._sources.get(source_id)
                if record is None:
                    raise ArchiveError(f"unknown source {source_id!r}")
                if self.resolve_id(record.shipment_id) != canonical:
                    raise ArchiveError(
                        f"source {source_id!r} belongs to a different shipment"
                    )
                chosen.append(record)
        material: list[MaterialUse] = []
        for record in chosen:
            event_ids = tuple(
                event.event_id
                for event in sorted(self._events.values(), key=lambda item: item.clause_index)
                if event.source_id == record.source_id
            )
            material.append(
                MaterialUse(
                    source_id=record.source_id,
                    content_hash=record.content_hash,
                    event_ids=event_ids,
                )
            )
        return tuple(material)

    def submissions_for(self, shipment_id: str) -> list[ClaimSubmission]:
        canonical = self.resolve_id(shipment_id)
        return sorted(
            (claim for claim in self._submissions.values()
             if self.resolve_id(claim.shipment_id) == canonical),
            key=lambda claim: claim.claim_id,
        )

    def submission_drift(self, claim: ClaimSubmission) -> list[str]:
        """Report material changed after a submitted version, without editing it."""

        drift: list[str] = []
        for version in claim.versions:
            if not version.digest_is_valid():
                drift.append(
                    f"{claim.claim_id} v{version.version}: 版本摘要校验失败，"
                    "档案被改动过，须人工比对原始提交"
                )
            for item in version.material:
                record = self._sources.get(item.source_id)
                if record is None:
                    drift.append(f"{claim.claim_id} v{version.version}: 来源 {item.source_id} 缺失")
                    continue
                if record.content_hash != item.content_hash:
                    drift.append(
                        f"{claim.claim_id} v{version.version}: 来源 {item.source_id} 内容哈希已变化"
                    )
                for event_id in item.event_ids:
                    event = self._events.get(event_id)
                    if event is None:
                        continue
                    if event.status is EventStatus.SUPERSEDED:
                        drift.append(
                            f"{claim.claim_id} v{version.version}: 材料 {item.source_id} 中的事件 "
                            f"{event_id}（「{_excerpt(event.text)}」）已被 "
                            f"{', '.join(event.superseded_by) or '更正记录'} 更正"
                        )
                    elif event.status is EventStatus.ATTRIBUTION_WITHDRAWN:
                        drift.append(
                            f"{claim.claim_id} v{version.version}: 材料 {item.source_id} 中的事件 "
                            f"{event_id}（「{_excerpt(event.text)}」）归属已撤回，"
                            "该段不应再作为本货件证据"
                        )
        already = {item.source_id for version in claim.versions for item in version.material}
        canonical = self.resolve_id(claim.shipment_id)
        submitted_at = timestamp_key(claim.original.created_at)
        later: list[str] = []
        earlier_unused: list[str] = []
        for record in self._sources.values():
            if self.resolve_id(record.shipment_id) != canonical or record.source_id in already:
                continue
            # "Later" means received after the original filing.  Material that
            # predates it but was not selected is a selection choice, not a
            # post-submission change, and is reported separately.
            if record.received_at and timestamp_key(record.received_at) > submitted_at:
                later.append(record.source_id)
            else:
                earlier_unused.append(record.source_id)
        if later:
            drift.append(
                "提交后新增材料未纳入: " + ", ".join(sorted(later)) + "（需通过补证版本加入）"
            )
        if earlier_unused:
            drift.append(
                "提交前已存在但未选用的材料: " + ", ".join(sorted(earlier_unused))
                + "（原版本未采用，可由处理人员判断是否补证）"
            )
        return drift

    # --------------------------------------------------------------- reviews
    def events_for(self, shipment_id: str) -> list[EvidenceEvent]:
        canonical = self.resolve_id(shipment_id)
        return sorted(
            (event for event in self._events.values()
             if self.resolve_id(event.shipment_id) == canonical),
            key=event_sort_key,
        )

    def _deadline_inputs_digest(self, shipment_id: str) -> str:
        events = self.events_for(shipment_id)
        assessment = deadline_assessment(events, policy=self.policy)
        return assessment.input_digest

    def _liability_inputs_digest(self, shipment_id: str) -> str:
        events = self.events_for(shipment_id)
        payload = [
            {
                "event_id": event.event_id,
                "status": event.status.value,
                "role": event.role.value,
                "mode": event.mode.value,
                "occurred_at": event.occurred_at,
            }
            for event in events
        ]
        return digest({"events": payload, "extractor": self.extractor_version})

    def record_review(
        self,
        *,
        shipment_id: str,
        kind: str,
        note: str,
        actor: str,
    ) -> ReviewCheckpoint:
        if kind not in {"deadline", "liability_draft"}:
            raise ArchiveError(f"unsupported review kind {kind!r}")
        canonical = self.resolve_id(shipment_id)
        payload_digest = (
            self._deadline_inputs_digest(shipment_id)
            if kind == "deadline"
            else self._liability_inputs_digest(shipment_id)
        )
        if kind == "deadline":
            assessment = deadline_assessment(self.events_for(shipment_id), policy=self.policy)
            basis = (
                f"起点 {assessment.chosen.event_id} / {assessment.chosen.source_id}"
                f" @ {assessment.chosen.occurred_at}; 到期 {assessment.due_at}; "
                f"状态 {assessment.status.value}"
                if assessment.chosen
                else "无可用起点"
            )
        else:
            basis = (
                f"事件 {len(self.events_for(shipment_id))} 条，"
                f"提取器 {self.extractor_version}"
            )
        checkpoint = ReviewCheckpoint(
            review_id=_stable_id(
                "review",
                {"shipment": canonical, "kind": kind, "digest": payload_digest, "actor": actor},
            ),
            shipment_id=canonical,
            kind=kind,
            digest=payload_digest,
            as_of_source_count=len(self.events_for(shipment_id)),
            note=note,
            actor=actor,
            basis=basis,
        )
        self._checkpoints.append(checkpoint)
        return checkpoint

    def latest_checkpoint(self, shipment_id: str, kind: str) -> ReviewCheckpoint | None:
        canonical = self.resolve_id(shipment_id)
        relevant = [
            checkpoint
            for checkpoint in self._checkpoints
            if checkpoint.kind == kind and self.resolve_id(checkpoint.shipment_id) == canonical
        ]
        if not relevant:
            return None
        return max(relevant, key=lambda item: (item.created_at, item.review_id))

    def review_state(
        self, shipment_id: str, kind: str
    ) -> tuple[ReviewState, str]:
        latest = self.latest_checkpoint(shipment_id, kind)
        if latest is None:
            return ReviewState.NOT_REVIEWED, "尚未记录复核 / no review checkpoint recorded"
        current = (
            self._deadline_inputs_digest(shipment_id)
            if kind == "deadline"
            else self._liability_inputs_digest(shipment_id)
        )
        if latest.digest != current:
            return (
                ReviewState.STALE,
                f"复核 {latest.review_id}（{latest.created_at}，{latest.actor}）的输入已变化，"
                f"当时依据: {latest.basis}；需重新核对 / inputs changed since checkpoint",
            )
        return ReviewState.CURRENT, (
            f"复核 {latest.review_id} 于 {latest.created_at} 有效（{latest.basis}）"
        )

    # ------------------------------------------------------------------ view
    def case_view(self, shipment_id: str, *, today: date | None = None) -> CaseView:
        canonical = self.resolve_id(shipment_id)
        sources = tuple(
            sorted(
                (record for record in self._sources.values()
                 if self.resolve_id(record.shipment_id) == canonical),
                key=lambda record: (timestamp_key(record.received_at), record.source_id),
            )
        )
        events = tuple(self.events_for(shipment_id))
        deadline = deadline_assessment(events, policy=self.policy, today=today)
        deadline_review, deadline_note = self.review_state(shipment_id, "deadline")
        liability_review, liability_note = self.review_state(shipment_id, "liability_draft")
        triggers = self._review_triggers(shipment_id)
        return CaseView(
            shipment_id=shipment_id,
            canonical_id=canonical,
            identity_warnings=tuple(self.identity_warnings()),
            links=tuple(
                link for link in self._links.values()
                if canonical in {self.resolve_id(link.old_id), self.resolve_id(link.new_id)}
                or shipment_id in {link.old_id, link.new_id}
            ),
            sources=sources,
            events=events,
            pending=tuple(self.pending_for(shipment_id)),
            submissions=tuple(self.submissions_for(shipment_id)),
            deadline=deadline,
            deadline_review=deadline_review,
            deadline_review_note=deadline_note,
            liability_review=liability_review,
            liability_review_note=liability_note,
            review_triggers=tuple(triggers),
            orphan_notes=tuple(self._orphan_notes(canonical)),
        )

    def _review_triggers(self, shipment_id: str) -> list[str]:
        triggers: list[str] = []
        canonical = self.resolve_id(shipment_id)
        correction_sources = [
            record for record in self._sources.values()
            if self.resolve_id(record.shipment_id) == canonical and record.is_correction
        ]
        for record in sorted(correction_sources, key=lambda item: timestamp_key(item.received_at)):
            triggers.append(
                f"{record.source_id} 为更正/修订记录（{record.kind}），依赖旧事件的判断需重核"
            )
        deadline_review, _ = self.review_state(shipment_id, "deadline")
        liability_review, _ = self.review_state(shipment_id, "liability_draft")
        if deadline_review is ReviewState.STALE:
            triggers.append("时限复核已过期，需重新确认起点和剩余天数")
        if liability_review is ReviewState.STALE:
            triggers.append("未提交责任判断草稿的输入已变化，需重新核对")
        if any(event.status is EventStatus.ATTRIBUTION_WITHDRAWN for event in self.events_for(shipment_id)):
            triggers.append("存在归属已撤回的扫描，原时间线中的异常环节需重排")
        return triggers

    def _orphan_notes(self, shipment_id: str) -> list[str]:
        notes: list[str] = []
        canonical = self.resolve_id(shipment_id)
        for record in self._sources.values():
            if self.resolve_id(record.shipment_id) != canonical:
                continue
            for reference in record.cross_references:
                if reference == record.shipment_id:
                    continue
                if reference not in {item.shipment_id for item in self._sources.values()}:
                    notes.append(
                        f"{record.source_id} 提及 {reference}，但档案中没有该货件，未自动并入"
                    )
        return notes

    def shipment_ids(self) -> list[str]:
        return sorted(
            {self.resolve_id(record.shipment_id) for record in self._sources.values()}
        )


def _normalise_id(value: str) -> str:
    return "".join(character for character in value.upper() if character.isalnum())


def _excerpt(text: str, limit: int = 32) -> str:
    cleaned = " ".join(text.split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


def _lookalike(left: str, right: str) -> bool:
    """Flag identifiers a human should compare, never merge.

    A pure numeric extension (SHP-1 vs SHP-10) is ordinary sequential
    numbering, not plausible renumbering, so it is excluded.  Alpha suffixes
    and single-character substitutions are flagged because carriers commonly
    reissue an identifier that way.
    """

    import difflib

    a, b = _normalise_id(left), _normalise_id(right)
    if not a or not b:
        return False
    short, long = sorted((a, b), key=len)
    if long.startswith(short) and long[len(short):].isdigit():
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.85


# Concept groups: a question using any variant matches a source using any
# other variant in the same group, across languages and scripts.
_QUESTION_CONCEPTS: tuple[tuple[str, ...], ...] = (
    ("交接", "移交", "handover", "handed over", "transfer", "custody"),
    ("签收", "簽收", "签字", "signature", "signed", "acknowledg", "receipt"),
    ("暂存", "暫存", "存放", "storage", "staging", "on hold", "holding"),
    ("扫描", "掃描", "刷读", "scan", "scanned", "manifest"),
    ("交付", "送达", "送達", "delivery", "delivered", "proof of delivery"),
    ("未到", "未收到", "未签收", "not delivered", "undelivered", "missing"),
)


def _question_terms(question: str) -> tuple[str, ...]:
    """Expand a question into concept variants so later sources can be surfaced."""

    lowered = question.lower()
    variants: list[str] = []
    for group in _QUESTION_CONCEPTS:
        if any(variant in lowered for variant in group):
            variants.extend(group)
    return tuple(dict.fromkeys(variants))
