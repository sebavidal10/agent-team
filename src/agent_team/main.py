from __future__ import annotations

import argparse
import time
from datetime import datetime
from pathlib import Path

from .config import load_settings
from .graph import build_graph
from .observability import ConsoleObserver
from .repo_context import build_role_snapshots
from .run_manager import init_run, save_context_files, save_manifest


def parse_args():
    parser = argparse.ArgumentParser(
        description="Audita un repositorio con un equipo multiagente local (v0.1.1)."
    )
    parser.add_argument("repo", type=Path, help="Ruta del repositorio a analizar")
    parser.add_argument(
        "--goal",
        default="Auditar el proyecto y definir una ruta concreta para terminar una v1 sólida.",
        help="Objetivo del equipo",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    settings = load_settings()

    run_ctx = init_run(settings.output_dir)
    observer = ConsoleObserver(log_file=run_ctx.log_file)

    snapshots = build_role_snapshots(
        args.repo,
        max_files=settings.max_files,
        max_file_chars=settings.max_file_chars,
        max_total_chars=settings.max_total_chars,
    )

    root = snapshots["architect"].root
    save_context_files(run_ctx, snapshots)

    all_unique_files = {
        f for snap in snapshots.values() for f in snap.files_included
    }

    # Start live visual pipeline
    observer.start_pipeline(
        repo_name=root.name,
        model=settings.model,
        total_unique_files=len(all_unique_files),
        snapshots=snapshots,
        num_ctx=settings.num_ctx,
    )

    start_time = time.time()
    graph = build_graph(settings, snapshots, run_ctx=run_ctx, observer=observer)

    result = graph.invoke({
        "goal": args.goal,
        "repo_name": root.name,
        "repo_tree": snapshots["architect"].tree,
        "repo_content": snapshots["architect"].content,
    })

    total_duration = time.time() - start_time
    reviewer_report = result.get("reviewer_report")
    role_metrics = result.get("role_metrics", {})

    if reviewer_report:
        specialists = [
            result.get("architect_report"),
            result.get("backend_report"),
            result.get("frontend_report"),
            result.get("testing_report"),
            result.get("docs_report"),
        ]
        valid_specs = [r for r in specialists if r is not None]
        from .models import compute_accounting_summary
        acc_summary = compute_accounting_summary(reviewer_report, valid_specs)

        save_manifest(
            run_ctx=run_ctx,
            repo_name=root.name,
            repo_path=str(root),
            goal=args.goal,
            model=settings.model,
            num_ctx=settings.num_ctx,
            role_metrics=role_metrics,
            reviewer_report=reviewer_report,
            finished_at=datetime.now(),
            accounting=acc_summary,
        )

    observer.finish_pipeline(
        repo_name=root.name,
        model=settings.model,
        total_duration=total_duration,
        role_metrics=role_metrics,
        reviewer_report=reviewer_report,
        run_dir=run_ctx.run_dir,
        final_report_path=run_ctx.final_report_file,
    )


if __name__ == "__main__":
    main()
