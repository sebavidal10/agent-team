from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import (
    BuilderOutput,
    ImprovementPlan,
    PatchProposal,
    ProjectBlueprint,
    ReviewerOutput,
    format_blueprint_markdown,
    format_final_guide_markdown,
    format_improvement_plan_markdown,
)


@dataclass
class RunContext:
    run_dir: Path
    patches_dir: Path
    reports_dir: Path
    log_file: Path
    manifest_file: Path
    final_guide_file: Path
    blueprint_file: Path
    plan_file: Path
    started_at: datetime = field(default_factory=datetime.now)

    def log(self, message: str) -> None:
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(message + "\n")
        except OSError:
            pass


def init_run(base_output_dir: Path, timestamp: str | None = None) -> RunContext:
    if not timestamp:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    run_dir = base_output_dir / f"run-{timestamp}"
    patches_dir = run_dir / "patches"
    reports_dir = run_dir / "reports"

    for d in [run_dir, patches_dir, reports_dir]:
        d.mkdir(parents=True, exist_ok=True)

    return RunContext(
        run_dir=run_dir,
        patches_dir=patches_dir,
        reports_dir=reports_dir,
        log_file=run_dir / "run.log",
        manifest_file=run_dir / "manifest.json",
        final_guide_file=run_dir / "final-guide.md",
        blueprint_file=run_dir / "project-blueprint.md",
        plan_file=run_dir / "improvement-plan.md",
        started_at=datetime.now(),
    )


def save_improvement_artifacts(
    run_ctx: RunContext,
    blueprint: ProjectBlueprint,
    plan: ImprovementPlan,
    builder_out: BuilderOutput,
    reviewer_out: ReviewerOutput,
) -> None:
    # 1. Save Project Blueprint
    bp_md = format_blueprint_markdown(blueprint)
    run_ctx.blueprint_file.write_text(bp_md, encoding="utf-8")
    (run_ctx.reports_dir / "blueprint.json").write_text(blueprint.model_dump_json(indent=2), encoding="utf-8")

    # 2. Save Improvement Plan
    plan_md = format_improvement_plan_markdown(plan)
    run_ctx.plan_file.write_text(plan_md, encoding="utf-8")
    (run_ctx.reports_dir / "plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")

    # 3. Save individual patches in patches/
    for idx, patch in enumerate(reviewer_out.validated_patches, 1):
        slug = re.sub(r"[^\w\-]", "_", patch.file_path).strip("_")
        patch_file = run_ctx.patches_dir / f"patch-{idx:02d}-{slug}.diff"
        patch_file.write_text(patch.diff_content, encoding="utf-8")

    (run_ctx.reports_dir / "builder.json").write_text(builder_out.model_dump_json(indent=2), encoding="utf-8")
    (run_ctx.reports_dir / "reviewer.json").write_text(reviewer_out.model_dump_json(indent=2), encoding="utf-8")

    # 4. Save Final Delivery Guide
    final_guide_md = format_final_guide_markdown(
        reviewer=reviewer_out,
        blueprint=blueprint,
        plan=plan,
        run_dir_name=run_ctx.run_dir.name,
    )
    run_ctx.final_guide_file.write_text(final_guide_md, encoding="utf-8")


def save_manifest(
    run_ctx: RunContext,
    repo_name: str,
    repo_path: str,
    goal: str,
    model: str,
    role_metrics: dict[str, Any],
    blueprint: ProjectBlueprint,
    plan: ImprovementPlan,
    patches_count: int,
    finished_at: datetime,
) -> None:
    total_seconds = (finished_at - run_ctx.started_at).total_seconds()

    manifest = {
        "repo_name": repo_name,
        "repo_path": repo_path,
        "goal": goal,
        "model": model,
        "started_at": run_ctx.started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "total_duration_seconds": round(total_seconds, 2),
        "patches_generated_count": patches_count,
        "blueprint": {
            "language": blueprint.primary_language,
            "framework": blueprint.framework,
            "libraries": blueprint.key_libraries,
        },
        "planned_improvements": [
            {"id": imp.id, "title": imp.title, "category": imp.category}
            for imp in plan.improvements
        ],
        "role_metrics": role_metrics,
    }

    run_ctx.manifest_file.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
