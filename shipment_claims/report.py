"""Human-readable case reports.

Chinese is the primary language because the reviewers work in Chinese; key
English source wording is preserved verbatim.  Every line that could be read as
a conclusion carries its mode (observation / reported / inference / formal
scan) and its source pointer.
"""

from __future__ import annotations

from datetime import date, datetime
from .archive import ClaimArchive
from .models import (
    CaseView,
    ConfirmationStatus,
    DeadlineStatus,
    EventStatus,
    EvidenceEvent,
    ReviewState,
    StatementMode,
    format_timestamp,
    parse_timestamp,
)

MODE_BADGE = {
    StatementMode.OBSERVED: "观察",
    StatementMode.REPORTED: "转述",
    StatementMode.INFERRED: "推断",
    StatementMode.SYSTEM_RECORD: "正式扫描",
    StatementMode.NOT_STATED: "未说明",
}

DEADLINE_BADGE = {
    DeadlineStatus.UNKNOWN: "未定",
    DeadlineStatus.OPEN: "进行中",
    DeadlineStatus.DUE_SOON: "临近",
    DeadlineStatus.EXPIRED: "已逾期(占位规则)",
}

REVIEW_BADGE = {
    ReviewState.NOT_REVIEWED: "未复核",
    ReviewState.CURRENT: "已复核",
    ReviewState.STALE: "已过期/需重核",
}


def _short_time(value: str | None, precision: str = "") -> str:
    parsed = parse_timestamp(value)
    if parsed is None:
        if precision == "time_only_unknown_date":
            return "仅有时刻、日期未知"
        return "时间未记录"
    label = format_timestamp(parsed)
    if precision in {"time_only", "date_from_text"}:
        label += "（时刻为来源原文，时区按收件记录）"
    return label


def _event_flag(event: EvidenceEvent) -> str:
    flags = [MODE_BADGE[event.mode]]
    if event.status is EventStatus.SUPERSEDED:
        flags.append("已被更正")
    if event.status is EventStatus.ATTRIBUTION_WITHDRAWN:
        flags.append("归属已撤回")
    if event.mode is StatementMode.INFERRED:
        flags.append("不可当已确认事实")
    return " / ".join(flags)


def render_timeline(view: CaseView, *, include_unknown: bool = True) -> list[str]:
    lines: list[str] = ["## 事件时间线 / Event timeline"]
    usable = [event for event in view.events if event.occurred_at or event.time_precision == "time_only_unknown_date"]
    undated = [event for event in view.events if not event.occurred_at and event.time_precision != "time_only_unknown_date"]
    undated.sort(key=lambda event: (event.source_id, event.clause_index))
    if not usable and not undated:
        lines.append("- 档案中没有可展示的事件。")
        return lines
    for event in usable:
        lines.extend(_render_event(event))
    if include_unknown and undated:
        lines.append("")
        lines.append("### 时间不明 / Undated assertions")
        for event in undated:
            lines.extend(_render_event(event))
    return lines


def _render_event(event: EvidenceEvent) -> list[str]:
    when = _short_time(event.occurred_at, event.time_precision)
    place = event.location or "地点未记录"
    who = f"{event.author}（{event.author_role}）"
    head = f"- **{when}** · {event.role_label} · {place} · {who} · {_event_flag(event)}"
    body = [head, f"  - 原文: 「{event.text}」", f"  - 来源: {event.source_id} ¶{event.clause_index + 1}"]
    if event.status is EventStatus.SUPERSEDED and event.superseded_by:
        body.append(f"  - 被更正为: {', '.join(event.superseded_by)}")
    if event.attribution_note:
        body.append(f"  - 说明: {event.attribution_note}")
    return body


def render_evidence(view: CaseView) -> list[str]:
    lines = ["## 证据来源 / Evidence sources"]
    if not view.sources:
        lines.append("- 无来源记录。")
        return lines
    for record in view.sources:
        lines.append(
            f"- `{record.source_id}` · {record.kind} · {record.author}（{record.author_role}）"
            f" · 地点: {record.location or '未记录'}"
            f" · 发生: {_short_time(record.observed_at, record.time_precision)}"
            f" · 收件: {_short_time(record.received_at)}"
        )
        lines.append(f"  - 原文（{record.lang}）: 「{record.text}」")
        if record.revision_of or record.corrects:
            lines.append(
                f"  - 修订关系: revision_of={record.revision_of or '—'} "
                f"corrects={', '.join(record.corrects) or '—'}"
                f" reassigns_to={record.reassigns_to or '—'}"
            )
        lines.append(f"  - 内容哈希: `{record.content_hash[:16]}…`")
    return lines


def render_deadline(view: CaseView) -> list[str]:
    assessment = view.deadline
    lines = ["## 时限复核 / Deadline review (占位规则，非法律意见)"]
    lines.append(
        f"- 状态: {DEADLINE_BADGE[assessment.status]} · {assessment.policy.policy_id} "
        f"v{assessment.policy.version} · {assessment.policy.days_from_handover} 天"
    )
    if assessment.chosen:
        chosen = assessment.chosen
        lines.append(
            f"- 采用起点: `{chosen.event_id}`（{chosen.source_id}，{chosen.role.value}）"
            f" {_short_time(chosen.occurred_at)}"
        )
        lines.append(f"- 起点依据: {chosen.basis}")
        lines.append(f"- 复核到期: {_short_time(assessment.due_at)}")
    else:
        lines.append("- 采用起点: 无（不能确定时限起算点）")
        lines.append(
            "- 起点依据: 没有同时具备时间、非推断、非转述且未被撤回的交接/交付事件"
        )
    lines.append("- 判断说明: " + assessment.policy.note)
    for reason in assessment.reasons:
        if reason == assessment.policy.note:
            continue
        lines.append(f"- 判断说明: {reason}")
    if assessment.candidates:
        lines.append("- 候选起点（未采用者保留理由）:")
        for candidate in assessment.candidates:
            mark = "采用" if candidate is assessment.chosen else ("可用" if candidate.usable else "排除")
            extra = f"；排除原因: {candidate.exclusion}" if candidate.exclusion else ""
            lines.append(
                f"  - [{mark}] `{candidate.event_id}` {candidate.role.value} "
                f"{_short_time(candidate.occurred_at)} · {candidate.basis}{extra}"
            )
    lines.append(
        f"- 复核状态: {REVIEW_BADGE[view.deadline_review]} — {view.deadline_review_note}"
    )
    return lines


def render_pending(view: CaseView) -> list[str]:
    lines = ["## 待承运商确认 / Open confirmations"]
    if not view.pending:
        lines.append("- 没有登记待确认事项。")
        return lines
    status_labels = {
        ConfirmationStatus.OPEN: "待确认",
        ConfirmationStatus.EVIDENCE_AVAILABLE: "已有新材料待复核",
        ConfirmationStatus.ANSWERED: "已答复",
        ConfirmationStatus.WITHDRAWN: "已撤回",
    }
    for item in view.pending:
        lines.append(
            f"- [{status_labels[item.status]}] `{item.question_id}` → {item.target_party}: "
            f"{item.question}"
        )
        if item.evidence_ids:
            lines.append(f"  - 相关材料: {', '.join(item.evidence_ids)}")
        if item.answer:
            lines.append(f"  - 答复（{item.answered_by}）: {item.answer}")
    still_open = [item for item in view.pending if item.status in {ConfirmationStatus.OPEN, ConfirmationStatus.EVIDENCE_AVAILABLE}]
    if still_open:
        lines.append("")
        lines.append(
            "**仍需承运商确认的环节: "
            + "；".join(item.question for item in still_open)
            + "**"
        )
    return lines


def render_submissions(view: CaseView, archive: ClaimArchive) -> list[str]:
    lines = ["## 已提交索赔资料 / Submitted material versions"]
    if not view.submissions:
        lines.append("- 尚未提交索赔资料（草稿阶段不自动提交）。")
        return lines
    for claim in view.submissions:
        lines.append(
            f"- 索赔 `{claim.claim_id}`（货件 {claim.carrier_ref or claim.shipment_id}）"
            f" · 状态 {claim.state}"
        )
        for version in claim.versions:
            kind = "原始提交" if version.kind == "original" else "补证"
            lines.append(
                f"  - v{version.version} {kind} · {version.created_at} · {version.created_by}"
                f" · 摘要: {version.summary}"
            )
            for item in version.material:
                lines.append(f"    - 采用材料 `{item.source_id}` @ `{item.content_hash[:16]}…`")
            lines.append(f"    - 版本摘要哈希: `{version.digest[:16]}…`")
        if len(claim.versions) > 1:
            lines.append("  - 版本间材料变化（原版本保持原样）:")
            lines.extend(f"    - {entry}" for entry in claim.use_history())
            original_ids = {item.source_id for item in claim.original.material}
            added = [
                item.source_id for version in claim.versions[1:] for item in version.material
            ]
            unchanged = sorted(original_ids - set(added))
            if added:
                lines.append(f"    - 补证新增: {', '.join(added)}")
            if unchanged:
                lines.append(f"    - 仍沿用原始版本材料: {', '.join(unchanged)}")
        drift = archive.submission_drift(claim)
        if drift:
            lines.append("  - 提交后变化（原版本不可改写，须以补证处理）:")
            for note in drift:
                lines.append(f"    - {note}")
    return lines


def render_review_state(view: CaseView) -> list[str]:
    lines = ["## 复核与提醒 / Review checkpoints"]
    lines.append(
        f"- 未提交责任判断: {REVIEW_BADGE[view.liability_review]} — {view.liability_review_note}"
    )
    lines.append(
        f"- 时限复核: {REVIEW_BADGE[view.deadline_review]} — {view.deadline_review_note}"
    )
    if view.review_triggers:
        lines.append("- 需重新核对:")
        for trigger in view.review_triggers:
            lines.append(f"  - {trigger}")
    else:
        lines.append("- 无新的重核触发项。")
    if view.orphan_notes:
        lines.append("- 未并入的编号:")
        for note in view.orphan_notes:
            lines.append(f"  - {note}")
    return lines


def render_identity(view: CaseView) -> list[str]:
    lines = ["## 货件编号 / Shipment identity"]
    lines.append(f"- 查询号: {view.shipment_id} → 归并号: {view.canonical_id}")
    if view.links:
        for link in view.links:
            lines.append(
                f"- 对应关系 `{link.link_id}`: {link.old_id} → {link.new_id} · "
                f"{link.status.value} · 依据 {', '.join(link.evidence_ids)} · {link.rationale}"
            )
    else:
        lines.append("- 无旧号/新号对应关系记录。")
    if view.identity_warnings:
        for warning in view.identity_warnings:
            lines.append(f"- ⚠ {warning}")
    return lines


def render_case(view: CaseView, archive: ClaimArchive) -> str:
    blocks: list[list[str]] = [
        [f"# 货件 {view.title} 案件材料"],
        [
            "> 本报告仅整理合成材料，不判断法律责任、不提交索赔。"
            "所有推断段落均明确标注，不能作为已确认事实引用。"
        ],
        render_identity(view),
        render_timeline(view),
        render_evidence(view),
        render_deadline(view),
        render_pending(view),
        render_submissions(view, archive),
        render_review_state(view),
    ]
    return "\n\n".join("\n".join(block) for block in blocks) + "\n"


def render_index(archive: ClaimArchive, *, today: date | None = None) -> str:
    lines = ["# 货件索引 / Shipment index", ""]
    for shipment_id in archive.shipment_ids():
        view = archive.case_view(shipment_id, today=today)
        open_pending = sum(
            1 for item in view.pending
            if item.status in {ConfirmationStatus.OPEN, ConfirmationStatus.EVIDENCE_AVAILABLE}
        )
        lines.append(
            f"- {shipment_id} · 来源 {len(view.sources)} · 事件 {len(view.events)} · "
            f"时限 {DEADLINE_BADGE[view.deadline.status]} · 待确认 {open_pending} · "
            f"复核 {REVIEW_BADGE[view.deadline_review]}"
        )
    warnings = archive.identity_warnings() + archive.similarity_warnings()
    if warnings:
        lines.append("")
        lines.append("## 编号提醒")
        for warning in warnings:
            lines.append(f"- ⚠ {warning}")
    return "\n".join(lines) + "\n"


def render_all(archive: ClaimArchive, *, today: date | None = None) -> str:
    parts = [render_index(archive, today=today)]
    for shipment_id in archive.shipment_ids():
        parts.append(render_case(archive.case_view(shipment_id, today=today), archive))
    return "\n\n---\n\n".join(parts)
