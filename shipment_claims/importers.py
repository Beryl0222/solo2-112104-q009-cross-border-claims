"""Import synthetic source bundles without silently guessing identities.

Two input shapes are accepted:

1. a JSON list of source records (legacy ``data/claims.json`` style), with an
   optional ``--shipment`` fallback that must be explicit
2. an envelope: ``{"shipments": {"SHP-1": [...]}, "links": [...]}``

Anything unassigned stays unassigned and is reported, never merged by string
similarity.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, Sequence

from .archive import ArchiveError, ClaimArchive
from .models import UNASSIGNED, build_source


def load_bundle(path: str | Path, *, default_shipment: str | None = None) -> ClaimArchive:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        return ClaimArchive.from_source_list(data, default_shipment=default_shipment)
    if not isinstance(data, Mapping):
        raise ArchiveError("input must be a JSON list or an object with shipments/links")

    archive = ClaimArchive()
    shipments = data.get("shipments")
    if shipments is not None:
        if not isinstance(shipments, Mapping):
            raise ArchiveError("'shipments' must map shipment ids to source lists")
        for shipment_id, records in shipments.items():
            for item in records:
                record = build_source(item, default_shipment=str(shipment_id))
                if record.shipment_id != str(shipment_id):
                    raise ArchiveError(
                        f"source {record.source_id} declares {record.shipment_id} but is filed "
                        f"under {shipment_id}; fix the source rather than remapping silently"
                    )
                archive.add_source(record)
    for item in data.get("sources", ()):
        archive.add_source(build_source(item, default_shipment=default_shipment))

    for link_data in data.get("links", ()):
        link = archive.proposed_link(
            str(link_data["old_id"]),
            str(link_data["new_id"]),
            evidence_ids=tuple(link_data.get("evidence_ids", ())),
            rationale=str(link_data.get("rationale", "imported proposal")),
            created_by=str(link_data.get("created_by", "import")),
        )
        if link_data.get("verify"):
            archive.verify_link(link.link_id, verified_by=str(link_data.get("created_by", "import")))

    for question in data.get("pending", ()):
        archive.add_pending(
            shipment_id=str(question["shipment_id"]),
            question=str(question["question"]),
            target_party=str(question.get("target_party", "carrier")),
            created_by=str(question.get("created_by", "import")),
            evidence_ids=tuple(question.get("evidence_ids", ())),
        )
    return archive


def unassigned_sources(archive: ClaimArchive) -> Sequence[str]:
    return tuple(
        record.source_id for record in archive.sources if record.shipment_id == UNASSIGNED
    )

