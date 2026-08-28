from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import AgentReport, ReviewerReport
from .repo_context import RepoSnapshot


@dataclass
class RunContext:
    run_dir: Path
    context_dir: Path
    reports_dir: Path
    markdown_dir: Path
    log_file: Path
    manifest_file: Path
    final_report_file: Path
    started_at: datetime = field(default_factory=datetime.now)

    def log(self, message: str) -> None:
        print(message)
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(message + "\n")
        except OSError:
            pass


def init_run(base_output_dir: Path, timestamp: str | None = None) -> RunContext:
    if not timestamp:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    run_dir = base_output_dir / f"run-{timestamp}"
    context_dir = run_dir / "context"
    reports_dir = run_dir / "reports"
    markdown_dir = run_dir / "markdown"

    for d in [run_dir, context_dir, reports_dir, markdown_dir]:
        d.mkdir(parents=True, exist_ok=True)

    return RunContext(
        run_dir=run_dir,
        context_dir=context_dir,
        reports_dir=reports_dir,
        markdown_dir=markdown_dir,
        log_file=run_dir / "run.log",
        manifest_file=run_dir / "manifest.json",
        final_report_file=run_dir / "final-report.md",
        started_at=datetime.now(),
    )


def save_context_files(run_ctx: RunContext, snapshots: dict[str, RepoSnapshot]) -> None:
    for role, snap in snapshots.items():
        ctx_file = run_ctx.context_dir / f"{role}-files.txt"
        content = "\n".join(snap.files_included)
        ctx_file.write_text(content, encoding="utf-8")


def save_specialist_output(
    run_ctx: RunContext,
    role: str,
    report: AgentReport,
) -> None:
    # Save JSON report
    json_path = run_ctx.reports_dir / f"{role}.json"
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    # Save Markdown report
    md_path = run_ctx.markdown_dir / f"{role}.md"
    md_path.write_text(report.to_markdown(), encoding="utf-8")


def save_reviewer_output(
    run_ctx: RunContext,
    reviewer_report: ReviewerReport,
    repo_name: str,
    goal: str,
    model_name: str,
    accounting: Any | None = None,
    specialist_statuses: dict[str, str] | None = None,
) -> None:
    # Save JSON report
    json_path = run_ctx.reports_dir / "reviewer.json"
    json_path.write_text(reviewer_report.model_dump_json(indent=2), encoding="utf-8")

    # Save Markdown report
    md_path = run_ctx.markdown_dir / "reviewer.md"
    final_md = reviewer_report.to_final_markdown(
        repo_name=repo_name,
        goal=goal,
        model_name=model_name,
        accounting=accounting,
        specialist_statuses=specialist_statuses,
    )
    md_path.write_text(final_md, encoding="utf-8")

    # Save final-report.md
    run_ctx.final_report_file.write_text(final_md, encoding="utf-8")


def save_manifest(
    run_ctx: RunContext,
    repo_name: str,
    repo_path: str,
    goal: str,
    model: str,
    role_metrics: dict[str, dict[str, Any]],
    reviewer_report: ReviewerReport,
    finished_at: datetime | None = None,
    accounting: Any | None = None,
    num_ctx: int | None = None,
) -> None:
    if not finished_at:
        finished_at = datetime.now()

    duration = (finished_at - run_ctx.started_at).total_seconds()

    all_files = set()
    for m in role_metrics.values():
        if "files_list" in m:
            all_files.update(m["files_list"])

    accounting_dict = {}
    if accounting:
        accounting_dict = {
            "total_input_findings": accounting.total_input_findings,
            "accepted_source_findings": accounting.accepted_count,
            "merged_source_findings": accounting.merged_count,
            "rejected_source_findings": accounting.rejected_count,
            "needs_verification_source_findings": accounting.needs_verification_count,
            "accounted_source_findings": accounting.accounted_count,
            "is_fully_accounted": accounting.is_fully_accounted,
        }

    manifest_data = {
        "repo_name": repo_name,
        "repo_path": repo_path,
        "goal": goal,
        "model": model,
        "num_ctx": num_ctx,
        "started_at": run_ctx.started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "total_duration_seconds": round(duration, 2),
        "unique_files_analyzed": len(all_files),
        "role_metrics": {
            k: {k2: v2 for k2, v2 in v.items() if k2 != "files_list"}
            for k, v in role_metrics.items()
        },
        "summary_findings_count": {
            "release_blockers": len(reviewer_report.release_blockers),
            "p0": len(reviewer_report.p0),
            "p1": len(reviewer_report.p1),
            "p2": len(reviewer_report.p2),
            "total_final_findings": len(reviewer_report.final_findings),
        },
        "findings_accounting": accounting_dict,
    }

    run_ctx.manifest_file.write_text(
        json.dumps(manifest_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
