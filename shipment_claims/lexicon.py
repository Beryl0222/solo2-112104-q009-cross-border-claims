"""Canonical event roles, statement modes, and extraction rules.

The extracted output is always an *assertion with an epistemic mode*, never a
bare fact.  This is what prevents a recipient's guess ("driver probably missed
the stop") from being reported as confirmed.

Rules are data-driven so reviewers can add phrases without changing parser
control flow.  Each rule matches one clause (an unpunctuated span) and yields
at most one event role, always with a :class:`StatementMode`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class EventRole(str, Enum):
    """What happened, independent of who said it."""

    HANDOVER = "handover"            # custody transfer between parties
    DELIVERY = "delivery"            # final delivery to recipient
    ACKNOWLEDGEMENT = "acknowledgement"  # signature / receipt confirmation
    NON_ACKNOWLEDGEMENT = "non_acknowledgement"  # explicitly unsigned
    STORAGE = "storage"              # temporary holding (暂存 / depot hold)
    SCAN = "scan"                    # a scan/manifest system record
    EXCEPTION = "exception"          # damage, loss, delay, refusal
    NON_DELIVERY = "non_delivery"    # explicitly not delivered
    UNKNOWN = "unknown"


class StatementMode(str, Enum):
    """How the content is known — the epistemic, not legal, status."""

    OBSERVED = "observation"         # author says they saw/did it
    REPORTED = "reported"            # author relays another party's statement
    INFERRED = "inference"           # speculation, guess, likely cause
    SYSTEM_RECORD = "formal_scan"    # scan / manifest / system record
    NOT_STATED = "not_stated"


class ClaimState(str, Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"


class PendingStatus(str, Enum):
    OPEN = "open"
    ANSWERED = "answered"
    WITHDRAWN = "withdrawn"


# Action/object vocabulary.  Longer phrases first so "proof of delivery"
# does not degrade to "delivery" alone in a misleading way.
ROLE_PATTERNS: tuple[tuple[str, EventRole], ...] = (
    # handover / custody transfer
    ("proof of delivery", EventRole.DELIVERY),
    ("proof of handover", EventRole.HANDOVER),
    ("handover", EventRole.HANDOVER),
    ("hand over", EventRole.HANDOVER),
    ("handed over", EventRole.HANDOVER),
    ("transfer", EventRole.HANDOVER),
    ("transferred", EventRole.HANDOVER),
    ("交接", EventRole.HANDOVER),
    ("移交", EventRole.HANDOVER),
    ("交出", EventRole.HANDOVER),
    ("转交", EventRole.HANDOVER),
    ("custody", EventRole.HANDOVER),
    ("釋放", EventRole.HANDOVER),
    ("release", EventRole.HANDOVER),
    # final delivery
    ("delivery", EventRole.DELIVERY),
    ("delivered", EventRole.DELIVERY),
    ("送達", EventRole.DELIVERY),
    ("送达", EventRole.DELIVERY),
    ("派送", EventRole.DELIVERY),
    ("投递", EventRole.DELIVERY),
    ("投遞", EventRole.DELIVERY),
    ("交貨", EventRole.DELIVERY),
    ("交货", EventRole.DELIVERY),
    ("drop off", EventRole.DELIVERY),
    ("dropped", EventRole.DELIVERY),
    # acknowledgement / signature
    ("signature", EventRole.ACKNOWLEDGEMENT),
    ("signed", EventRole.ACKNOWLEDGEMENT),
    ("acknowledg", EventRole.ACKNOWLEDGEMENT),
    ("receipt", EventRole.ACKNOWLEDGEMENT),
    ("签收", EventRole.ACKNOWLEDGEMENT),
    ("簽收", EventRole.ACKNOWLEDGEMENT),
    ("签字", EventRole.ACKNOWLEDGEMENT),
    ("簽字", EventRole.ACKNOWLEDGEMENT),
    # storage / temporary hold
    ("temporary storage", EventRole.STORAGE),
    ("on hold", EventRole.STORAGE),
    ("held at", EventRole.STORAGE),
    ("holding", EventRole.STORAGE),
    ("staging", EventRole.STORAGE),
    ("暫存", EventRole.STORAGE),
    ("暂存", EventRole.STORAGE),
    ("寄放", EventRole.STORAGE),
    ("存放", EventRole.STORAGE),
    ("留置", EventRole.STORAGE),
    # scans / formal records
    ("scan", EventRole.SCAN),
    ("manifest", EventRole.SCAN),
    ("scanned", EventRole.SCAN),
    ("掃描", EventRole.SCAN),
    ("扫描", EventRole.SCAN),
    ("刷讀", EventRole.SCAN),
    ("刷读", EventRole.SCAN),
    ("系統記錄", EventRole.SCAN),
    ("系统记录", EventRole.SCAN),
    # exceptions
    ("damage", EventRole.EXCEPTION),
    ("damaged", EventRole.EXCEPTION),
    ("shortage", EventRole.EXCEPTION),
    ("loss", EventRole.EXCEPTION),
    ("lost", EventRole.EXCEPTION),
    ("refused", EventRole.EXCEPTION),
    ("refusal", EventRole.EXCEPTION),
    ("delay", EventRole.EXCEPTION),
    ("delayed", EventRole.EXCEPTION),
    ("破损", EventRole.EXCEPTION),
    ("破損", EventRole.EXCEPTION),
    ("丢失", EventRole.EXCEPTION),
    ("遺失", EventRole.EXCEPTION),
    ("短少", EventRole.EXCEPTION),
    ("拒收", EventRole.EXCEPTION),
    ("延误", EventRole.EXCEPTION),
    ("延誤", EventRole.EXCEPTION),
    # explicit non-delivery
    ("not delivered", EventRole.NON_DELIVERY),
    ("undelivered", EventRole.NON_DELIVERY),
    ("未到", EventRole.NON_DELIVERY),
    ("未送達", EventRole.NON_DELIVERY),
    ("未送达", EventRole.NON_DELIVERY),
    ("未收到", EventRole.NON_DELIVERY),
)

# Phrases that shift the statement into inference.  These are checked before
# plain observation so "猜测...漏送" becomes inference, not a delivery event.
INFERENCE_MARKERS: tuple[str, ...] = (
    "猜测", "猜測", "推测", "推測", "估计", "估計", "疑似", "可能",
    "也许", "也許", "大概", "或许", "或許", "按理", "应该是", "應該是",
    "probably", "likely", "presumably", "appears", "seems", "might",
    "may have", "suspect", "guess", "assume", "possibly", "perhaps",
    "allegedly", "apparently",
)

# Phrases that mark relayed speech rather than the author's observation.
REPORTED_MARKERS: tuple[str, ...] = (
    "邮件称", "郵件稱", "邮件说", "郵件說", "来函称", "來函稱",
    "据称", "據稱", "据司机", "據司機", "对方称", "對方稱",
    "声称", "聲稱", "转述", "轉述", "转告", "轉告", "告知",
    "says", "said", "states", "stated", "reports", "reported",
    "claims", "claimed", "according to", "per the", "advised",
    "told us", "informed",
)

# Formal system evidence.  A clause carrying these is SYSTEM_RECORD unless
# it is itself clearly an inference.
SYSTEM_MARKERS: tuple[str, ...] = (
    "scan", "scanned", "manifest", "system record", "system shows",
    "tracking shows", "scan record", "corrected scan",
    "扫描", "掃描", "系统记录", "系統記錄", "轨迹", "軌跡", "清单", "清單",
)

# Negation markers applied after role matching.  A negated delivery/handover
# is represented as NON_DELIVERY, and a negated acknowledgement as
# NON_ACKNOWLEDGEMENT, not the positive action.
NEGATION_PATTERNS: tuple[str, ...] = (
    "未", "没有", "沒有", "无", "無", "尚无", "尚無", "not ", "no ",
    "without", "never", "n't",
)

# Correction / replacement cues.  These make a record a revision source.
CORRECTION_MARKERS: tuple[str, ...] = (
    "corrected", "correction", "revised", "amended", "supersedes",
    "replaces", "prior", "previous", "invalid", "void", "error",
    "更正", "修正", "纠正", "糾正", "勘誤", "勘误", "原记录", "原記錄",
    "先前", "撤销", "撤銷", "作废", "作廢", "错误", "錯誤",
)

# Deadline policy.  These are *review triggers*, not legal opinions.
DEFAULT_DEADLINE_DAYS = 14
DEADLINE_START_ROLES: tuple[EventRole, ...] = (
    EventRole.DELIVERY,
    EventRole.HANDOVER,
    EventRole.NON_DELIVERY,
    EventRole.EXCEPTION,
)

# Localised labels for reports.
ROLE_LABELS: dict[EventRole, str] = {
    EventRole.HANDOVER: "交接 / handover",
    EventRole.DELIVERY: "交付 / delivery",
    EventRole.ACKNOWLEDGEMENT: "签收 / acknowledgement",
    EventRole.NON_ACKNOWLEDGEMENT: "未签收 / no acknowledgement",
    EventRole.STORAGE: "暂存 / storage",
    EventRole.SCAN: "扫描 / scan",
    EventRole.EXCEPTION: "异常 / exception",
    EventRole.NON_DELIVERY: "未交付 / non-delivery",
    EventRole.UNKNOWN: "未归类 / unknown",
}

MODE_LABELS: dict[StatementMode, str] = {
    StatementMode.OBSERVED: "观察 / observation",
    StatementMode.REPORTED: "转述 / reported",
    StatementMode.INFERRED: "推断 / inference",
    StatementMode.SYSTEM_RECORD: "正式扫描记录 / formal scan",
    StatementMode.NOT_STATED: "未说明 / not stated",
}

# Chinese commas are clause boundaries; English commas are not, so dates and
# place names such as "East Depot, Gate 3" stay intact.
CLAUSE_SPLIT_RE = re.compile(r"(?<=[。；;！!？?，、])|(?<=\.)\s+|\n+")


@dataclass(frozen=True)
class Clause:
    """One sentence-like span with its extraction result."""

    text: str
    role: EventRole
    mode: StatementMode
    matched_phrases: tuple[str, ...]

    @property
    def is_inference(self) -> bool:
        return self.mode is StatementMode.INFERRED


def find_markers(text: str, markers: tuple[str, ...]) -> tuple[str, ...]:
    """Case-insensitive marker lookup preserving the matched phrase."""

    lowered = text.lower()
    hits: list[str] = []
    for marker in markers:
        if marker.lower() in lowered and marker not in hits:
            hits.append(marker)
    return tuple(hits)


def split_clauses(text: str) -> list[str]:
    """Split text into clauses while retaining separators until stripped."""

    pieces = CLAUSE_SPLIT_RE.split(text)
    return [piece.strip() for piece in pieces if piece and piece.strip()]


NEGATIVE_ROLE = {
    EventRole.ACKNOWLEDGEMENT: EventRole.NON_ACKNOWLEDGEMENT,
    EventRole.DELIVERY: EventRole.NON_DELIVERY,
    EventRole.HANDOVER: EventRole.NON_DELIVERY,
}

# Words between a negation and the role phrase that still count as negating it.
NEGATION_GAP_ZH = 4
NEGATION_GAP_EN = 24


def _negation_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    lowered = text.lower()
    for marker in NEGATION_PATTERNS:
        needle = marker.lower()
        start = 0
        while True:
            index = lowered.find(needle, start)
            if index == -1:
                break
            spans.append((index, index + len(needle)))
            start = index + len(needle)
    return spans


def _role_is_negated(text: str, phrase: str) -> bool:
    """Whether a negation marker *immediately precedes* this role phrase.

    Proximity is used rather than clause-wide matching so that "放在暂存区，
    值班员未签收" keeps the storage observation while recording the missing
    signature separately.  Only preceding negation counts: in "handover, not
    proof of delivery" the "not" belongs to delivery, not to handover.
    """

    lowered = text.lower()
    phrase_start = lowered.find(phrase.lower())
    if phrase_start == -1:
        return False
    for neg_start, neg_end in _negation_spans(text):
        if neg_end > phrase_start:
            continue
        gap = phrase_start - neg_end
        limit = NEGATION_GAP_ZH if _is_cjk(phrase) else NEGATION_GAP_EN
        if gap <= limit:
            return True
    return False


def _is_cjk(text: str) -> bool:
    return any("一" <= character <= "鿿" for character in text)


def match_role(text: str) -> tuple[EventRole, tuple[str, ...]]:
    """Return the most specific matching role, downgrading negated positives."""

    lowered = text.lower()
    positive: list[tuple[int, str, EventRole]] = []
    negative: list[tuple[int, str, EventRole]] = []
    for phrase, role in ROLE_PATTERNS:
        if phrase not in lowered:
            continue
        if role in {EventRole.NON_DELIVERY, EventRole.UNKNOWN, EventRole.EXCEPTION}:
            positive.append((len(phrase), phrase, role))
            continue
        if _role_is_negated(text, phrase):
            negative.append((len(phrase), phrase, NEGATIVE_ROLE.get(role, EventRole.NON_DELIVERY)))
        else:
            positive.append((len(phrase), phrase, role))
    pool = positive or negative
    if not pool:
        return EventRole.UNKNOWN, ()
    # Longest phrase wins; ties keep the earlier (more canonical) rule.
    pool.sort(key=lambda item: (-item[0],))
    best_role = pool[0][2]
    matched = tuple(
        sorted({phrase for _, phrase, role in pool if role is best_role}, key=len, reverse=True)
    )
    return best_role, matched


def classify_mode(text: str, kind: str) -> tuple[StatementMode, tuple[str, ...]]:
    """Determine the epistemic mode of a clause.

    Ordering matters: inference beats system record (a speculative reading of
    a scan is still speculation), and formal scan sources beat ordinary
    reported speech because the record itself is the evidence.
    """

    inference_hits = find_markers(text, INFERENCE_MARKERS)
    reported_hits = find_markers(text, REPORTED_MARKERS)
    system_hits = find_markers(text, SYSTEM_MARKERS)

    if inference_hits:
        return StatementMode.INFERRED, inference_hits
    if kind in {"scan", "scan_correction", "manifest"} or system_hits:
        return StatementMode.SYSTEM_RECORD, system_hits
    if kind in {"recipient_email", "carrier_message"} and system_hits:
        return StatementMode.SYSTEM_RECORD, system_hits
    if reported_hits:
        return StatementMode.REPORTED, reported_hits
    if kind in {"driver_note", "dock_note", "warehouse_note"}:
        return StatementMode.OBSERVED, ()
    return StatementMode.NOT_STATED, ()


def extract_clauses(text: str, kind: str) -> list[Clause]:
    """Extract clause-level assertions from one source record."""

    clauses: list[Clause] = []
    for raw in split_clauses(text):
        role, role_hits = match_role(raw)
        mode, _ = classify_mode(raw, kind)
        clauses.append(
            Clause(text=raw, role=role, mode=mode, matched_phrases=role_hits)
        )
    return clauses
