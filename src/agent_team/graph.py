from __future__ import annotations

import operator
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langchain_ollama import ChatOllama
from langgraph.graph import END, START, StateGraph

from .config import Settings
from .models import (
    BuilderOutput,
    ImprovementPlan,
    PatchProposal,
    ProjectBlueprint,
    ReviewerOutput,
    parse_builder_output,
    parse_improvement_plan,
    parse_project_blueprint,
    parse_reviewer_output,
)
from .observability import ConsoleObserver
from .repo_context import (
    RepoSnapshot,
    build_builder_snapshot,
    build_global_inventory_tree,
    build_planner_snapshot,
    build_profiler_snapshot,
)
from .run_manager import RunContext


class TeamState(TypedDict, total=False):
    goal: str
    repo_name: str
    repo_path: str
    blueprint: ProjectBlueprint
    improvement_plan: ImprovementPlan
    builder_output: BuilderOutput
    reviewer_output: ReviewerOutput
    role_metrics: Annotated[dict[str, dict[str, Any]], operator.or_]


def _read_prompt(settings: Settings, name: str) -> str:
    path = settings.prompts_dir / f"{name}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def _extract_telemetry(
    raw_msg: Any,
    duration: float,
    num_ctx: int | None = None,
    num_predict: int | None = None,
    role: str | None = None,
    run_ctx: RunContext | None = None,
) -> dict[str, Any]:
    meta = {}
    if hasattr(raw_msg, "response_metadata") and isinstance(raw_msg.response_metadata, dict):
        meta = raw_msg.response_metadata
    elif isinstance(raw_msg, dict) and "response_metadata" in raw_msg:
        meta = raw_msg.get("response_metadata", {})

    prompt_eval = meta.get("prompt_eval_count")
    eval_count = meta.get("eval_count")

    return {
        "duration_seconds": round(duration, 2),
        "prompt_eval_count": prompt_eval,
        "eval_count": eval_count,
        "output_truncated": bool(eval_count and num_predict and eval_count >= num_predict),
    }


def build_graph(
    settings: Settings,
    root_dir: Path,
    run_ctx: RunContext | None = None,
    observer: ConsoleObserver | None = None,
):
    llm = ChatOllama(
        model=settings.model,
        base_url=settings.base_url,
        temperature=0.1,
        num_ctx=settings.num_ctx,
        num_predict=settings.num_predict,
        timeout=settings.timeout_seconds,
    )

    shared_standards = _read_prompt(settings, "_shared_standards")

    # =================================================================
    # 1. Profiler Node
    # =================================================================
    def profiler_node(state: TeamState):
        role = "profiler"
        t0 = time.time()
        snap = build_profiler_snapshot(root_dir, max_file_chars=settings.max_file_chars)

        if observer:
            observer.start_agent(
                role=role,
                files_count=len(snap.files_included),
                context_chars=snap.total_chars,
                model=settings.model,
                discarded_files=snap.candidates_discarded,
            )
            observer.phase_start(role, f"Analyzing architecture and manifests ({settings.model})...")

        prompt_tpl = _read_prompt(settings, "profiler")
        system_prompt = f"{prompt_tpl}\n\n---\n{shared_standards}" if shared_standards else prompt_tpl

        user_content = f"""PROYECTO LOCAL: {root_dir.name}

ÁRBOL DE DIRECTORIOS Y ARCHIVOS:
{snap.tree}

CONTENIDO DE ARCHIVOS DE CONFIGURACIÓN Y MANIFIESTOS ({len(snap.files_included)} archivos):
{snap.content}

Genera el Project Blueprint estructurado en JSON.
"""
        messages = [
            ("system", system_prompt),
            ("human", user_content),
        ]

        raw_text = ""
        blueprint = None
        duration = 0.0
        telemetry = {}

        try:
            att_t0 = time.time()
            res = llm.invoke(messages)
            duration = time.time() - att_t0
            raw_text = res.content if hasattr(res, "content") else str(res)
            telemetry = _extract_telemetry(res, duration, settings.num_ctx, settings.num_predict, role, run_ctx)
            blueprint = parse_project_blueprint(raw_text, project_name=root_dir.name)
        except Exception as e:
            if run_ctx:
                run_ctx.log(f"Error in profiler node: {e}")
            blueprint = parse_project_blueprint({}, project_name=root_dir.name)

        if observer:
            observer.phase_done(role, "Blueprint generated")
            observer.finish_agent(role=role, duration=duration, findings_count=0, status="valid")

        metrics = {
            role: {
                "duration": duration,
                "files_count": len(snap.files_included),
                "context_chars": snap.total_chars,
                "status": "valid",
                "telemetry": telemetry,
            }
        }

        return {
            "blueprint": blueprint,
            "role_metrics": metrics,
        }

    # =================================================================
    # 2. Planner Node
    # =================================================================
    def planner_node(state: TeamState):
        role = "planner"
        t0 = time.time()
        blueprint: ProjectBlueprint = state.get("blueprint") or parse_project_blueprint({}, project_name=root_dir.name)
        goal = state.get("goal") or "Mejorar la calidad y robustez del proyecto"

        snap = build_planner_snapshot(root_dir, max_file_chars=settings.max_file_chars)

        if observer:
            observer.start_agent(
                role=role,
                files_count=len(snap.files_included),
                context_chars=snap.total_chars,
                model=settings.model,
                discarded_files=snap.candidates_discarded,
            )
            observer.phase_start(role, f"Planning targeted improvements ({settings.model})...")

        prompt_tpl = _read_prompt(settings, "planner")
        system_prompt = f"{prompt_tpl}\n\n---\n{shared_standards}" if shared_standards else prompt_tpl

        bp_summary = f"""PROJECT BLUEPRINT:
- Lenguaje: {blueprint.primary_language}
- Framework: {blueprint.framework}
- Librerías: {', '.join(blueprint.key_libraries)}
- Convenciones: {'; '.join(blueprint.code_conventions)}
- Resumen: {blueprint.summary}
"""

        user_content = f"""OBJETIVO DE MEJORA DEL USUARIO:
{goal}

{bp_summary}

ÁRBOL COMPLETO DEL PROYECTO:
{snap.tree}

Diseña un plan de 2 a 4 mejoras de alto impacto con archivos objetivo específicos delimitados en 'target_files'. Responde en JSON.
"""
        messages = [
            ("system", system_prompt),
            ("human", user_content),
        ]

        raw_text = ""
        plan = None
        duration = 0.0
        telemetry = {}

        try:
            att_t0 = time.time()
            res = llm.invoke(messages)
            duration = time.time() - att_t0
            raw_text = res.content if hasattr(res, "content") else str(res)
            telemetry = _extract_telemetry(res, duration, settings.num_ctx, settings.num_predict, role, run_ctx)
            plan = parse_improvement_plan(raw_text, goal=goal)
        except Exception as e:
            if run_ctx:
                run_ctx.log(f"Error in planner node: {e}")
            plan = parse_improvement_plan({}, goal=goal)

        if observer:
            observer.phase_done(role, f"{len(plan.improvements)} improvements planned")
            observer.finish_agent(role=role, duration=duration, findings_count=len(plan.improvements), status="valid")

        metrics = {
            role: {
                "duration": duration,
                "files_count": len(snap.files_included),
                "context_chars": snap.total_chars,
                "status": "valid",
                "telemetry": telemetry,
            }
        }

        return {
            "improvement_plan": plan,
            "role_metrics": metrics,
        }

    # =================================================================
    # 3. Builder Node
    # =================================================================
    def builder_node(state: TeamState):
        role = "builder"
        t0 = time.time()
        blueprint: ProjectBlueprint = state.get("blueprint") or parse_project_blueprint({}, project_name=root_dir.name)
        plan: ImprovementPlan = state.get("improvement_plan") or parse_improvement_plan({}, goal=state.get("goal", ""))

        target_files = []
        for imp in plan.improvements:
            target_files.extend(imp.target_files)

        snap = build_builder_snapshot(root_dir, target_files=target_files)

        if observer:
            observer.start_agent(
                role=role,
                files_count=len(snap.files_included),
                context_chars=snap.total_chars,
                model=settings.model,
                discarded_files=snap.candidates_discarded,
            )
            observer.phase_start(role, f"Writing code patches in unified diff ({settings.model})...")

        prompt_tpl = _read_prompt(settings, "builder")
        system_prompt = f"{prompt_tpl}\n\n---\n{shared_standards}" if shared_standards else prompt_tpl

        plan_desc = []
        for imp in plan.improvements:
            plan_desc.append(
                f"- [{imp.id}] {imp.title} ({imp.category})\n"
                f"  Archivos objetivo: {', '.join(imp.target_files)}\n"
                f"  Justificación: {imp.rationale}\n"
                f"  Pasos: {'; '.join(imp.implementation_steps)}"
            )

        user_content = f"""PROJECT BLUEPRINT (Convenciones a respetar):
- Lenguaje: {blueprint.primary_language}
- Framework: {blueprint.framework}
- Convenciones: {'; '.join(blueprint.code_conventions)}

MEJORAS A IMPLEMENTAR:
{chr(10).join(plan_desc)}

CONTENIDO ACTUAL DE LOS ARCHIVOS OBJETIVO:
{snap.content}

Genera los parches de código exactos en formato unified diff para cada mejora. Responde en JSON.
"""
        messages = [
            ("system", system_prompt),
            ("human", user_content),
        ]

        raw_text = ""
        builder_out = None
        duration = 0.0
        telemetry = {}

        try:
            att_t0 = time.time()
            res = llm.invoke(messages)
            duration = time.time() - att_t0
            raw_text = res.content if hasattr(res, "content") else str(res)
            telemetry = _extract_telemetry(res, duration, settings.num_ctx, settings.num_predict, role, run_ctx)
            builder_out = parse_builder_output(raw_text)
        except Exception as e:
            if run_ctx:
                run_ctx.log(f"Error in builder node: {e}")
            builder_out = parse_builder_output({})

        if observer:
            observer.phase_done(role, f"{len(builder_out.patches)} patches generated")
            observer.finish_agent(role=role, duration=duration, findings_count=len(builder_out.patches), status="valid")

        metrics = {
            role: {
                "duration": duration,
                "files_count": len(snap.files_included),
                "context_chars": snap.total_chars,
                "status": "valid",
                "telemetry": telemetry,
            }
        }

        return {
            "builder_output": builder_out,
            "role_metrics": metrics,
        }

    # =================================================================
    # 4. Reviewer Node
    # =================================================================
    def reviewer_node(state: TeamState):
        role = "reviewer"
        t0 = time.time()
        blueprint: ProjectBlueprint = state.get("blueprint") or parse_project_blueprint({}, project_name=root_dir.name)
        plan: ImprovementPlan = state.get("improvement_plan") or parse_improvement_plan({}, goal=state.get("goal", ""))
        builder_out: BuilderOutput = state.get("builder_output") or parse_builder_output({})

        patches_count = len(builder_out.patches)

        if observer:
            observer.start_agent(
                role=role,
                files_count=patches_count,
                context_chars=sum(len(p.diff_content) for p in builder_out.patches),
                model=settings.get_reviewer_model(),
                discarded_files=0,
            )
            observer.phase_start(role, f"Validating patches and creating delivery guide ({settings.get_reviewer_model()})...")

        prompt_tpl = _read_prompt(settings, "reviewer")
        system_prompt = f"{prompt_tpl}\n\n---\n{shared_standards}" if shared_standards else prompt_tpl

        patches_desc = []
        for idx, p in enumerate(builder_out.patches, 1):
            patches_desc.append(
                f"### Parche {idx}: [{p.improvement_id}] {p.title}\n"
                f"Archivo: {p.file_path} (Acción: {p.action})\n"
                f"```diff\n{p.diff_content}\n```\n"
            )

        user_content = f"""PROJECT BLUEPRINT:
- Proyecto: {blueprint.project_name} ({blueprint.primary_language} / {blueprint.framework})
- Testing Setup: {blueprint.test_setup or 'No configurado'}

OBJETIVO GENERAL:
{plan.goal}

PARCHES GENERADOS POR EL BUILDER ({patches_count} parches):
{chr(10).join(patches_desc) if patches_desc else "No se generaron parches de código."}

Valida la coherencia de los parches y elabora la guía paso a paso para aplicarlos con 'git apply' y verificar su funcionamiento. Responde en JSON.
"""
        messages = [
            ("system", system_prompt),
            ("human", user_content),
        ]

        reviewer_llm = llm
        if settings.get_reviewer_model() != settings.model:
            reviewer_llm = ChatOllama(
                model=settings.get_reviewer_model(),
                base_url=settings.base_url,
                temperature=0.1,
                num_ctx=settings.num_ctx,
                num_predict=settings.num_predict,
                timeout=settings.timeout_seconds,
            )

        raw_text = ""
        rev_out = None
        duration = 0.0
        telemetry = {}

        try:
            att_t0 = time.time()
            res = reviewer_llm.invoke(messages)
            duration = time.time() - att_t0
            raw_text = res.content if hasattr(res, "content") else str(res)
            telemetry = _extract_telemetry(res, duration, settings.num_ctx, settings.num_predict, role, run_ctx)
            rev_out = parse_reviewer_output(raw_text, builder_output=builder_out)
        except Exception as e:
            if run_ctx:
                run_ctx.log(f"Error in reviewer node: {e}")
            rev_out = parse_reviewer_output({}, builder_output=builder_out)

        if observer:
            observer.phase_done(role, "Review guide completed")
            observer.finish_agent(role=role, duration=duration, findings_count=len(rev_out.validated_patches), status="valid")

        metrics = {
            role: {
                "duration": duration,
                "files_count": len(rev_out.validated_patches),
                "context_chars": sum(len(p.diff_content) for p in rev_out.validated_patches),
                "status": "valid",
                "telemetry": telemetry,
            }
        }

        return {
            "reviewer_output": rev_out,
            "role_metrics": metrics,
        }

    # =================================================================
    # Graph Assembly
    # =================================================================
    graph = StateGraph(TeamState)
    graph.add_node("profiler", profiler_node)
    graph.add_node("planner", planner_node)
    graph.add_node("builder", builder_node)
    graph.add_node("reviewer", reviewer_node)

    graph.add_edge(START, "profiler")
    graph.add_edge("profiler", "planner")
    graph.add_edge("planner", "builder")
    graph.add_edge("builder", "reviewer")
    graph.add_edge("reviewer", END)

    return graph.compile()
