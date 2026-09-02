from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator

from .repo_context import normalize_rel_path


def repair_json_string(raw: str) -> str:
    """
    Cleans and repairs common malformations in LLM JSON output:
    - Extracts JSON payload from markdown fences or text wrappers
    - Normalizes smart quotes (“ ” ‘ ’)
    - Strips trailing commas before } and ]
    - Removes zero-width or non-printable control characters
    """
    if not raw or not isinstance(raw, str):
        return ""

    s = raw.strip()

    # 1. Extract content from inside ```json ... ``` or ``` ... ``` if present
    fence_match = re.search(r"```(?:json)?\s*([\{\[].*?[\}\]])\s*```", s, re.DOTALL)
    if fence_match:
        s = fence_match.group(1).strip()
    else:
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()

    # 2. Extract outermost { ... } or [ ... ] if wrapped with surrounding text
    first_brace = s.find("{")
    first_bracket = s.find("[")
    if first_brace != -1 or first_bracket != -1:
        start_candidates = [idx for idx in [first_brace, first_bracket] if idx != -1]
        start = min(start_candidates)
        last_brace = s.rfind("}")
        last_bracket = s.rfind("]")
        end = max(last_brace, last_bracket)
        if end > start:
            s = s[start:end+1]

    # 3. Normalize smart quotes to standard JSON double quotes
    s = s.replace("“", '"').replace("”", '"').replace("„", '"').replace("‟", '"')
    s = s.replace("‘", "'").replace("’", "'")

    # 4. Remove trailing commas before } or ]
    for _ in range(4):
        s = re.sub(r",\s*([\]\}])", r"\1", s)

    # 5. Remove non-standard control characters (keep \t, \n, \r)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", s)

    return s.strip()


_PLACEHOLDERS = {
    "n/a", "na", "none", "unknown", "null", "nil",
    "sin información", "sin informacion", "no aplica",
    "no disponible", "no se especificó", "no se especifico",
}


def is_meaningful_text(v: Any, min_len: int = 3) -> bool:
    """Returns True if the value is non-empty, non-whitespace, and not a placeholder."""
    if not isinstance(v, str):
        return False
    v_clean = v.strip()
    if len(v_clean) < min_len:
        return False
    if v_clean.lower() in _PLACEHOLDERS:
        return False
    if all(ch in "-_.:;/* \t\n…" for ch in v_clean):
        return False
    return True


# =====================================================================
# 1. Project Blueprint (Profiler Output)
# =====================================================================

class ProjectBlueprint(BaseModel):
    project_name: str = Field(..., description="Nombre del proyecto detectado")
    primary_language: str = Field(..., description="Lenguaje principal (ej. TypeScript, Python)")
    framework: str = Field(default="None", description="Framework principal (ej. Next.js, FastAPI)")
    key_libraries: list[str] = Field(default_factory=list, description="Librerías principales instaladas")
    architecture_style: str = Field(default="", description="Estilo arquitectónico (ej. Monorepo, MVC, Clean Architecture)")
    code_conventions: list[str] = Field(default_factory=list, description="Convenciones de código y estilo detectadas")
    test_setup: str | None = Field(default=None, description="Setup de testing configurado si existe")
    summary: str = Field(default="", description="Resumen descriptivo del proyecto y su arquitectura")

    @field_validator("project_name", "primary_language", mode="before")
    @classmethod
    def clean_strings(cls, v: Any) -> str:
        return str(v).strip() if v else "unknown"

    @field_validator("key_libraries", "code_conventions", mode="before")
    @classmethod
    def clean_lists(cls, v: Any) -> list[str]:
        if isinstance(v, list):
            return [str(item).strip() for item in v if str(item).strip()]
        if isinstance(v, str) and v.strip():
            return [line.strip("- *").strip() for line in v.splitlines() if line.strip()]
        return []


def parse_project_blueprint(raw: str | dict[str, Any], project_name: str = "local-project") -> ProjectBlueprint:
    """Safely parses raw text or dict into ProjectBlueprint with fallback."""
    if isinstance(raw, dict):
        try:
            return ProjectBlueprint.model_validate(raw)
        except Exception:
            pass

    raw_str = str(raw) if raw is not None else ""
    repaired = repair_json_string(raw_str)
    if repaired:
        try:
            data = json.loads(repaired)
            if isinstance(data, dict):
                return ProjectBlueprint.model_validate(data)
        except Exception:
            pass

    # Fallback blueprint
    return ProjectBlueprint(
        project_name=project_name,
        primary_language="Detected from files",
        framework="Detected from dependencies",
        key_libraries=[],
        architecture_style="Modular",
        code_conventions=["Follow existing project patterns"],
        test_setup=None,
        summary="Blueprint generado automáticamente a partir del escaneo inicial de archivos.",
    )


# =====================================================================
# 2. Improvement Plan (Planner Output)
# =====================================================================

class ImprovementItem(BaseModel):
    id: str = Field(..., description="Identificador único (ej. IMP-01)")
    title: str = Field(..., description="Título de la mejora")
    category: str = Field(default="Refactor", description="Categoría (Security, Reliability, Refactor, Feature, Testing, DX)")
    target_files: list[str] = Field(default_factory=list, description="Archivos específicos a modificar o crear")
    rationale: str = Field(default="", description="Justificación técnica de la mejora")
    expected_impact: str = Field(default="", description="Impacto esperado en el proyecto")
    implementation_steps: list[str] = Field(default_factory=list, description="Pasos exactos de implementación")

    @field_validator("target_files", mode="before")
    @classmethod
    def clean_target_files(cls, v: Any) -> list[str]:
        if isinstance(v, list):
            return [normalize_rel_path(str(p)) for p in v if str(p).strip()]
        if isinstance(v, str) and v.strip():
            return [normalize_rel_path(v.strip())]
        return []


class ImprovementPlan(BaseModel):
    goal: str = Field(..., description="Objetivo perseguido")
    summary: str = Field(default="", description="Resumen de la estrategia de mejoras planificadas")
    improvements: list[ImprovementItem] = Field(default_factory=list, description="Lista de 2 a 4 mejoras concretas")


def parse_improvement_plan(raw: str | dict[str, Any], goal: str = "") -> ImprovementPlan:
    """Safely parses raw text or dict into ImprovementPlan with fallback."""
    if isinstance(raw, dict):
        try:
            return ImprovementPlan.model_validate(raw)
        except Exception:
            pass

    raw_str = str(raw) if raw is not None else ""
    repaired = repair_json_string(raw_str)
    if repaired:
        try:
            data = json.loads(repaired)
            if isinstance(data, dict):
                if not data.get("goal") and goal:
                    data["goal"] = goal
                return ImprovementPlan.model_validate(data)
        except Exception:
            pass

    # Fallback plan
    return ImprovementPlan(
        goal=goal or "Mejorar la estabilidad y arquitectura del proyecto",
        summary="Plan de mejoras generado a partir del objetivo.",
        improvements=[
            ImprovementItem(
                id="IMP-01",
                title="Consolidar validaciones y manejo de errores",
                category="Reliability",
                target_files=[],
                rationale="Garantizar que las entradas y errores se manejen de forma predecible.",
                expected_impact="Mayor estabilidad y robustez.",
                implementation_steps=["Revisar puntos críticos de entrada y agregar validaciones."],
            )
        ],
    )


# =====================================================================
# 3. Patch Proposals & Builder Output
# =====================================================================

class PatchProposal(BaseModel):
    improvement_id: str = Field(default="IMP-01", description="ID de la mejora asociada")
    title: str = Field(..., description="Título descriptivo del parche")
    file_path: str = Field(..., description="Ruta relativa del archivo afectado")
    action: Literal["modify", "create", "delete"] = Field(default="modify", description="Acción del parche")
    diff_content: str = Field(..., description="Contenido en formato unified diff o contenido nuevo")
    explanation: str = Field(default="", description="Explicación concisa del cambio")

    @field_validator("file_path", mode="before")
    @classmethod
    def clean_file_path(cls, v: Any) -> str:
        return normalize_rel_path(str(v)) if v else "unknown"


class BuilderOutput(BaseModel):
    summary: str = Field(default="", description="Resumen de los parches implementados")
    patches: list[PatchProposal] = Field(default_factory=list, description="Lista de parches generados")


def extract_unified_diffs(raw_text: str) -> list[PatchProposal]:
    """
    Extracts unified diff blocks from raw text (e.g. inside ```diff ... ```).
    """
    proposals: list[PatchProposal] = []
    if not raw_text:
        return proposals

    diff_blocks = re.findall(r"```(?:diff)?\s*(---.*?)\s*```", raw_text, re.DOTALL)
    for idx, block in enumerate(diff_blocks, 1):
        target_file = "patch.diff"
        file_match = re.search(r"\+\+\+\s+(?:[bB]/)?([^\s\n]+)", block)
        if file_match:
            target_file = normalize_rel_path(file_match.group(1))

        proposals.append(
            PatchProposal(
                improvement_id=f"IMP-{idx:02d}",
                title=f"Parche para {target_file}",
                file_path=target_file,
                action="modify",
                diff_content=block.strip(),
                explanation="Parche de código extraído automáticamente.",
            )
        )
    return proposals


def parse_builder_output(raw: str | dict[str, Any]) -> BuilderOutput:
    """Safely parses raw text or dict into BuilderOutput with fallback."""
    if isinstance(raw, dict):
        try:
            return BuilderOutput.model_validate(raw)
        except Exception:
            pass

    raw_str = str(raw) if raw is not None else ""
    repaired = repair_json_string(raw_str)
    if repaired:
        try:
            data = json.loads(repaired)
            if isinstance(data, dict):
                return BuilderOutput.model_validate(data)
        except Exception:
            pass

    # Extract any diff blocks directly from markdown if JSON parsing fails
    diff_proposals = extract_unified_diffs(raw_str)
    return BuilderOutput(
        summary="Parches de código generados por el Builder.",
        patches=diff_proposals,
    )


# =====================================================================
# 4. Reviewer Output & Delivery Guide
# =====================================================================

class ReviewerOutput(BaseModel):
    overall_summary: str = Field(..., description="Evaluación general de las mejoras y su impacto")
    review_status: Literal["approved", "approved_with_notes", "needs_revision"] = Field(
        default="approved", description="Estado de revisión de los parches"
    )
    validated_patches: list[PatchProposal] = Field(default_factory=list, description="Parches validados listos para aplicar")
    step_by_step_guide: list[str] = Field(default_factory=list, description="Instrucciones paso a paso para aplicar y probar")
    verification_checklist: list[str] = Field(default_factory=list, description="Lista de comprobaciones manuales o automáticas")
    warnings_or_notes: list[str] = Field(default_factory=list, description="Advertencias o notas importantes")


def parse_reviewer_output(
    raw: str | dict[str, Any],
    builder_output: BuilderOutput | None = None,
) -> ReviewerOutput:
    """Safely parses raw text or dict into ReviewerOutput with fallback."""
    if isinstance(raw, dict):
        try:
            return ReviewerOutput.model_validate(raw)
        except Exception:
            pass

    raw_str = str(raw) if raw is not None else ""
    repaired = repair_json_string(raw_str)
    if repaired:
        try:
            data = json.loads(repaired)
            if isinstance(data, dict):
                res = ReviewerOutput.model_validate(data)
                if not res.validated_patches and builder_output and builder_output.patches:
                    res.validated_patches = builder_output.patches
                return res
        except Exception:
            pass

    # Fallback reviewer output using builder patches
    patches = builder_output.patches if builder_output else []
    guide = [
        "Paso 1: Revisa los parches generados en el directorio output/<run>/patches/",
        "Paso 2: Aplica los parches deseados con: git apply <ruta-al-parche>",
        "Paso 3: Ejecuta las pruebas del proyecto para validar la integración.",
    ]
    return ReviewerOutput(
        overall_summary="Las mejoras planificadas han sido implementadas en parches de código listos para su revisión.",
        review_status="approved",
        validated_patches=patches,
        step_by_step_guide=guide,
        verification_checklist=[
            "Verificar que el proyecto compile y arranque sin errores.",
            "Confirmar que los tests existentes sigan pasando.",
        ],
        warnings_or_notes=[],
    )


# =====================================================================
# Markdown Formatters
# =====================================================================

def format_blueprint_markdown(bp: ProjectBlueprint) -> str:
    libs = ", ".join(f"`{lib}`" for lib in bp.key_libraries) if bp.key_libraries else "Ninguna detectada"
    convs = "\n".join(f"- {c}" for c in bp.code_conventions) if bp.code_conventions else "- Seguir patrones estándar del proyecto"
    tests = bp.test_setup or "No configurado o no detectado"

    return f"""# Project Blueprint — {bp.project_name}

- **Lenguaje Principal:** {bp.primary_language}
- **Framework:** {bp.framework}
- **Estilo Arquitectónico:** {bp.architecture_style or "Modular"}
- **Librerías Clave:** {libs}
- **Setup de Testing:** {tests}

## Resumen Arquitectónico
{bp.summary}

## Convenciones de Código y Estilo
{convs}
"""


def format_improvement_plan_markdown(plan: ImprovementPlan) -> str:
    lines = [
        f"# Plan de Mejoras — {plan.goal}",
        "",
        f"**Resumen:** {plan.summary}",
        "",
        "## Mejoras Planificadas",
        "",
    ]
    for idx, item in enumerate(plan.improvements, 1):
        files_str = ", ".join(f"`{f}`" for f in item.target_files) if item.target_files else "Archivos generales"
        steps_str = "\n".join(f"  {s_idx}. {step}" for s_idx, step in enumerate(item.implementation_steps, 1)) if item.implementation_steps else "  - Implementar cambios según justificación."
        lines.extend([
            f"### [{item.id}] {item.title} ({item.category})",
            f"- **Archivos Objetivo:** {files_str}",
            f"- **Justificación:** {item.rationale}",
            f"- **Impacto Esperado:** {item.expected_impact}",
            f"- **Pasos:**",
            steps_str,
            "",
        ])
    return "\n".join(lines)


def format_final_guide_markdown(
    reviewer: ReviewerOutput,
    blueprint: ProjectBlueprint | None = None,
    plan: ImprovementPlan | None = None,
    run_dir_name: str = "run-latest",
) -> str:
    proj_title = blueprint.project_name if blueprint else "Proyecto Local"
    goal_title = plan.goal if plan else "Mejoras de Código"

    lines = [
        f"# Guía de Aplicación de Mejoras — {proj_title}",
        "",
        f"- **Objetivo:** {goal_title}",
        f"- **Estado de Revisión:** {reviewer.review_status.upper()}",
        f"- **Parches Generados:** {len(reviewer.validated_patches)}",
        "",
        "## Resumen Ejecutivo",
        reviewer.overall_summary,
        "",
        "## Parches de Código Listos para Aplicar",
        "",
    ]

    for idx, p in enumerate(reviewer.validated_patches, 1):
        lines.extend([
            f"### Parche {idx:02d}: [{p.improvement_id}] {p.title}",
            f"- **Archivo Afectado:** `{p.file_path}` (Acción: `{p.action}`)",
            f"- **Explicación:** {p.explanation}",
            "",
            "```diff",
            p.diff_content,
            "```",
            "",
        ])

    lines.extend([
        "## Pasos para Aplicar las Mejoras",
        "",
    ])
    for step in reviewer.step_by_step_guide:
        lines.append(f"- {step}")
    lines.append("")

    if reviewer.verification_checklist:
        lines.extend([
            "## Checklist de Verificación",
            "",
        ])
        for check in reviewer.verification_checklist:
            lines.append(f"- [ ] {check}")
        lines.append("")

    if reviewer.warnings_or_notes:
        lines.extend([
            "## Notas y Advertencias",
            "",
        ])
        for note in reviewer.warnings_or_notes:
            lines.append(f"> ⚠️ {note}")
        lines.append("")

    return "\n".join(lines)
