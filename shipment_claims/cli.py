"""Command-line entry point for archive, review, and report operations."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .archive import ArchiveError, ClaimArchive
from .importers import load_bundle
from .report import render_all, render_case


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m shipment_claims",
        description="整理合成跨境物流叙述，保留证据来源与复核状态（不判断法律责任）。",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--archive", type=Path, help="已导出的档案 JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    import_cmd = sub.add_parser(
        "import",
        parents=[common],
        help="导入合成来源并保存档案")
    import_cmd.add_argument("bundle", type=Path)
    import_cmd.add_argument("--shipment", help="列表输入的显式货件号（不猜测）")
    import_cmd.add_argument("--out", type=Path, required=True)

    report_cmd = sub.add_parser(
        "report",
        parents=[common],
        help="输出案件材料 Markdown")
    report_cmd.add_argument("--shipment", help="只输出一个货件；缺省输出全部")
    report_cmd.add_argument("--today", help="评估日 YYYY-MM-DD（默认今天）")
    report_cmd.add_argument("--out", type=Path)

    json_cmd = sub.add_parser(
        "json",
        parents=[common],
        help="输出结构化案件视图")
    json_cmd.add_argument("--shipment", required=True)
    json_cmd.add_argument("--today", help="评估日 YYYY-MM-DD")

    pending_cmd = sub.add_parser(
        "ask",
        parents=[common],
        help="登记待承运商确认事项")
    pending_cmd.add_argument("--shipment", required=True)
    pending_cmd.add_argument("--question", required=True)
    pending_cmd.add_argument("--target", default="carrier")
    pending_cmd.add_argument("--actor", required=True)
    pending_cmd.add_argument("--evidence", nargs="*", default=())

    answer_cmd = sub.add_parser(
        "answer",
        parents=[common],
        help="记录承运商答复")
    answer_cmd.add_argument("--question-id", required=True)
    answer_cmd.add_argument("--answer", required=True)
    answer_cmd.add_argument("--actor", required=True)
    answer_cmd.add_argument("--source", nargs="*", default=())

    link_cmd = sub.add_parser(
        "link",
        parents=[common],
        help="登记旧号→新号对应关系")
    link_cmd.add_argument("--old", required=True)
    link_cmd.add_argument("--new", required=True)
    link_cmd.add_argument("--evidence", nargs="+", required=True)
    link_cmd.add_argument("--rationale", required=True)
    link_cmd.add_argument("--actor", required=True)
    link_cmd.add_argument("--verify", action="store_true", help="人工复核后核验（仍检查证据）")

    merge_cmd = sub.add_parser(
        "merge",
        parents=[common],
        help="按已核验对应关系确认合并")
    merge_cmd.add_argument("--old", required=True)
    merge_cmd.add_argument("--new", required=True)
    merge_cmd.add_argument("--link-id", required=True)

    review_cmd = sub.add_parser(
        "review",
        parents=[common],
        help="记录时限或未提交责任草稿的复核")
    review_cmd.add_argument("--shipment", required=True)
    review_cmd.add_argument("--kind", choices=["deadline", "liability_draft"], required=True)
    review_cmd.add_argument("--note", required=True)
    review_cmd.add_argument("--actor", required=True)

    submit_cmd = sub.add_parser(
        "submit",
        parents=[common],
        help="登记已提交的索赔资料版本（不可改写）")
    submit_cmd.add_argument("--claim-id", required=True)
    submit_cmd.add_argument("--shipment", required=True)
    submit_cmd.add_argument("--summary", required=True)
    submit_cmd.add_argument("--actor", required=True)
    submit_cmd.add_argument("--source", nargs="*", default=None)
    submit_cmd.add_argument("--carrier-ref")

    supplement_cmd = sub.add_parser(
        "supplement",
        parents=[common],
        help="为已提交索赔追加补证版本")
    supplement_cmd.add_argument("--claim-id", required=True)
    supplement_cmd.add_argument("--summary", required=True)
    supplement_cmd.add_argument("--actor", required=True)
    supplement_cmd.add_argument("--source", nargs="*", default=None)
    return parser


def _load(args: argparse.Namespace) -> ClaimArchive:
    if not args.archive:
        raise SystemExit("--archive is required for this command")
    return ClaimArchive.from_export(args.archive)


def _save(archive: ClaimArchive, path: Path) -> None:
    archive.save(path)


def _parse_today(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "import":
            archive = load_bundle(args.bundle, default_shipment=args.shipment)
            if not archive.sources:
                raise ArchiveError("bundle contained no source records")
            _save(archive, args.out)
            print(f"archived {len(archive.sources)} sources -> {args.out}")
            return 0

        if args.command == "report":
            archive = _load(args)
            today = _parse_today(args.today)
            if args.shipment:
                text = render_case(archive.case_view(args.shipment, today=today), archive)
            else:
                text = render_all(archive, today=today)
            if args.out:
                args.out.write_text(text, encoding="utf-8")
                print(f"wrote {args.out}")
            else:
                sys.stdout.write(text)
            return 0

        if args.command == "json":
            archive = _load(args)
            view = archive.case_view(args.shipment, today=_parse_today(args.today))
            print(json.dumps(view.to_dict(), ensure_ascii=False, indent=2))
            return 0

        if args.command == "ask":
            archive = _load(args)
            item = archive.add_pending(
                shipment_id=args.shipment,
                question=args.question,
                target_party=args.target,
                created_by=args.actor,
                evidence_ids=args.evidence,
            )
            _save(archive, args.archive)
            print(item.question_id)
            return 0

        if args.command == "answer":
            archive = _load(args)
            item = archive.answer_pending(
                args.question_id, answer=args.answer, answered_by=args.actor,
                source_ids=args.source,
            )
            _save(archive, args.archive)
            print(f"{item.question_id} -> {item.status.value}")
            return 0

        if args.command == "link":
            archive = _load(args)
            link = archive.proposed_link(
                args.old,
                args.new,
                evidence_ids=args.evidence,
                rationale=args.rationale,
                created_by=args.actor,
            )
            if args.verify:
                link = archive.verify_link(link.link_id, verified_by=args.actor)
            _save(archive, args.archive)
            print(f"{link.link_id} {link.status.value}")
            return 0

        if args.command == "merge":
            archive = _load(args)
            archive.merge_shipment(args.old, args.new, link_id=args.link_id)
            _save(archive, args.archive)
            print(f"verified link {args.link_id}: {args.old} -> {args.new}")
            return 0

        if args.command == "review":
            archive = _load(args)
            checkpoint = archive.record_review(
                shipment_id=args.shipment, kind=args.kind, note=args.note, actor=args.actor
            )
            _save(archive, args.archive)
            print(f"{checkpoint.review_id} {checkpoint.kind}")
            return 0

        if args.command == "submit":
            archive = _load(args)
            claim = archive.submit_claim(
                claim_id=args.claim_id,
                shipment_id=args.shipment,
                summary=args.summary,
                created_by=args.actor,
                source_ids=args.source,
                carrier_ref=args.carrier_ref,
            )
            _save(archive, args.archive)
            print(f"{claim.claim_id} v{claim.latest.version} submitted")
            return 0

        if args.command == "supplement":
            archive = _load(args)
            claim = archive.supplement_claim(
                args.claim_id,
                summary=args.summary,
                created_by=args.actor,
                source_ids=args.source,
            )
            _save(archive, args.archive)
            print(f"{claim.claim_id} v{claim.latest.version} appended")
            return 0
    except ArchiveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    parser.error("unhandled command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
