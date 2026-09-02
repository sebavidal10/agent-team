from __future__ import annotations

import argparse
from dataclasses import replace
import sys
import time
from datetime import datetime
from pathlib import Path

from .config import load_settings
from .graph import build_graph
from .models import (
    BuilderOutput,
    ImprovementPlan,
    ProjectBlueprint,
    ReviewerOutput,
)
from .observability import ConsoleObserver
from .run_manager import (
    init_run,
    save_improvement_artifacts,
    save_manifest,
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Equipo multiagente local para mejorar proyectos (Profiler -> Planner -> Builder -> Reviewer)."
    )
    parser.add_argument("repo", type=Path, help="Ruta del proyecto local a mejorar")
    parser.add_argument(
        "--goal",
        default="Analizar la arquitectura del proyecto y proponer parches de código para resolver debilidades clave.",
        help="Objetivo de mejora para el equipo",
    )
    parser.add_argument(
        "--reviewer-model",
        default=None,
        help="Modelo específico para el Tech Lead / Reviewer (ej. qwen2.5-coder:7b o llama3.1:8b)",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        default=False,
        help="Habilitar confirmación interactiva de aplicación de parches al finalizar",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    settings = load_settings()

    target_repo = args.repo.resolve()
    if not target_repo.exists() or not target_repo.is_dir():
        print(f"Error: La ruta del proyecto '{args.repo}' no existe o no es un directorio válido.")
        sys.exit(1)

    overrides = {}
    if args.reviewer_model:
        overrides["reviewer_model"] = args.reviewer_model
    if args.interactive:
        overrides["interactive"] = True
    if overrides:
        settings = replace(settings, **overrides)

    run_ctx = init_run(settings.output_dir)
    observer = ConsoleObserver(log_file=run_ctx.log_file)

    observer.start_pipeline(
        repo_name=target_repo.name,
        model=settings.model,
        total_unique_files=0,
        snapshots=None,
        num_ctx=settings.num_ctx,
    )

    start_time = time.time()
    graph = build_graph(settings, target_repo, run_ctx=run_ctx, observer=observer)

    result = graph.invoke({
        "goal": args.goal,
        "repo_name": target_repo.name,
        "repo_path": str(target_repo),
    })

    total_duration = time.time() - start_time

    blueprint: ProjectBlueprint = result.get("blueprint")
    plan: ImprovementPlan = result.get("improvement_plan")
    builder_out: BuilderOutput = result.get("builder_output")
    reviewer_out: ReviewerOutput = result.get("reviewer_output")
    role_metrics = result.get("role_metrics", {})

    patches_count = len(reviewer_out.validated_patches) if reviewer_out else 0
    review_status = reviewer_out.review_status if reviewer_out else "unknown"

    if blueprint and plan and builder_out and reviewer_out:
        save_improvement_artifacts(
            run_ctx=run_ctx,
            blueprint=blueprint,
            plan=plan,
            builder_out=builder_out,
            reviewer_out=reviewer_out,
        )

        save_manifest(
            run_ctx=run_ctx,
            repo_name=target_repo.name,
            repo_path=str(target_repo),
            goal=args.goal,
            model=settings.model,
            role_metrics=role_metrics,
            blueprint=blueprint,
            plan=plan,
            patches_count=patches_count,
            finished_at=datetime.now(),
        )

    observer.finish_pipeline(
        repo_name=target_repo.name,
        model=settings.model,
        total_duration=total_duration,
        role_metrics=role_metrics,
        patches_count=patches_count,
        review_status=review_status,
        run_dir=run_ctx.run_dir,
        final_guide_path=run_ctx.final_guide_file,
    )

    if settings.interactive and reviewer_out and reviewer_out.validated_patches:
        if sys.stdin.isatty():
            print("\n" + "=" * 65)
            print("🛠️ MODO INTERACTIVO: Aplicación de Parches Generados")
            print("=" * 65)
            for idx, patch in enumerate(reviewer_out.validated_patches, 1):
                print(f"\n[{idx}/{len(reviewer_out.validated_patches)}] {patch.title}")
                print(f"Archivo: {patch.file_path} (Acción: {patch.action})")
                print(f"Explicación: {patch.explanation}")
                try:
                    ans = input("¿Deseas ver el comando para aplicar este parche? (s/N): ").strip().lower()
                    if ans in ("s", "si", "y", "yes"):
                        patch_slug = patch.file_path.replace("/", "_")
                        print(f"  Comando: git apply {run_ctx.patches_dir}/patch-{idx:02d}-{patch_slug}.diff")
                except (EOFError, KeyboardInterrupt):
                    print("\nSesión interactiva finalizada.")
                    break


if __name__ == "__main__":
    main()
