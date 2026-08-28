from __future__ import annotations

import json
import re
from dataclasses import dataclass
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
    "n/a",
    "na",
    "none",
    "unknown",
    "null",
    "nil",
    "sin información",
    "sin informacion",
    "no aplica",
    "no disponible",
    "no se especificó",
    "no se especifico",
    "hallazgo sin título",
    "hallazgo sin titulo",
    "título del hallazgo",
    "titulo del hallazgo",
    "título corto del hallazgo",
    "titulo corto del hallazgo",
    "título del hallazgo backend",
    "titulo del hallazgo backend",
    "título del hallazgo frontend",
    "titulo del hallazgo frontend",
    "título del hallazgo de testing",
    "titulo del hallazgo de testing",
    "título del hallazgo de documentación",
    "titulo del hallazgo de documentacion",
}


def is_meaningful_text(v: Any, min_len: int = 3) -> bool:
    """Returns True if the value is non-empty, non-whitespace, and not a placeholder or template string."""
    if not isinstance(v, str):
        return False
    v_clean = v.strip()
    if len(v_clean) < min_len:
        return False
    v_lower = v_clean.lower().strip()
    if v_lower in _PLACEHOLDERS:
        return False
    if all(ch in "-_.:;/* \t\n…" for ch in v_clean):
        return False
    # Check for template ellipsis endings or obvious template phrases
    if v_clean.endswith("...") or v_clean.endswith("…"):
        if len(v_clean) < 70 and any(
            v_lower.startswith(p) for p in [
                "resumen", "pregunta", "descripci", "hallazgo", "título", "titulo",
                "sin hallazgo", "n/a", "evaluaci", "criterio", "orden"
            ]
        ):
            return False
    # Exact placeholder phrases
    if v_lower in {
        "sin hallazgos", "sin hallazgos.", "sin hallazgos relevantes",
        "no hay hallazgos", "no se detectaron hallazgos", "...", "n/a"
    }:
        return False
    return True


class SpecialistFindingLLM(BaseModel):
    priority: Literal["P0", "P1", "P2", "P3"] = "P2"
    title: str = Field(default="")
    evidence: str = Field(default="")
    files: list[str] = Field(default_factory=list)
    impact: str = Field(default="")
    recommendation: str = Field(default="")
    confidence: Literal["high", "medium", "low"] = "medium"


class SpecialistLLMOutput(BaseModel):
    summary: str = Field(default="")
    no_findings_reason: str | None = Field(
        default=None,
        description="Si findings está vacío ([]), explica detalladamente qué se revisó y por qué no existen problemas sustentados."
    )
    findings: list[SpecialistFindingLLM] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class ReviewerFinalFindingLLM(BaseModel):
    source_finding_ids: list[str] = Field(default_factory=list)
    priority: Literal["P0", "P1", "P2", "P3"] = "P2"
    source_priority: str | None = None
    reprioritization_reason: str | None = None
    title: str = Field(default="")
    evidence: str = Field(default="")
    files: list[str] = Field(default_factory=list)
    impact: str = Field(default="")
    recommendation: str = Field(default="")
    confidence: Literal["high", "medium", "low"] = "medium"


class ReviewerUnresolvedSourceLLM(BaseModel):
    source_finding_ids: list[str] = Field(default_factory=list)
    disposition: Literal["rejected", "needs_verification"] = "rejected"
    reason: str = Field(default="")

    @field_validator("disposition", mode="before")
    @classmethod
    def normalize_disposition(cls, v: Any) -> str:
        if isinstance(v, str):
            v_l = v.lower().strip()
            if "reject" in v_l or "descart" in v_l or "rechaz" in v_l:
                return "rejected"
            if "verif" in v_l or "duda" in v_l or "pendient" in v_l:
                return "needs_verification"
        return "rejected"

    @field_validator("source_finding_ids", mode="before")
    @classmethod
    def normalize_source_ids(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            cleaned = v.strip()
            if cleaned and cleaned.lower() not in _PLACEHOLDERS:
                return [cleaned]
            return []
        if isinstance(v, (list, tuple, set)):
            return [
                str(item).strip()
                for item in v
                if str(item).strip() and str(item).strip().lower() not in _PLACEHOLDERS
            ]
        return []


class ReviewerContradictionLLM(BaseModel):
    source_finding_ids: list[str] = Field(default_factory=list)
    description: str = Field(default="")

    @field_validator("source_finding_ids", mode="before")
    @classmethod
    def normalize_source_ids(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            cleaned = v.strip()
            if cleaned and cleaned.lower() not in _PLACEHOLDERS:
                return [cleaned]
            return []
        if isinstance(v, (list, tuple, set)):
            return [
                str(item).strip()
                for item in v
                if str(item).strip() and str(item).strip().lower() not in _PLACEHOLDERS
            ]
        return []


class ReviewerDiscardedClaimLLM(BaseModel):
    source_finding_ids: list[str] = Field(default_factory=list)
    reason: str = Field(default="")

    @field_validator("source_finding_ids", mode="before")
    @classmethod
    def normalize_source_ids(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            cleaned = v.strip()
            if cleaned and cleaned.lower() not in _PLACEHOLDERS:
                return [cleaned]
            return []
        if isinstance(v, (list, tuple, set)):
            return [
                str(item).strip()
                for item in v
                if str(item).strip() and str(item).strip().lower() not in _PLACEHOLDERS
            ]
        return []


class ReviewerLLMOutput(BaseModel):
    summary: str = Field(default="")
    v1_readiness: Literal["ready", "not_ready", "needs_verification"] = "needs_verification"
    v1_readiness_reason: str = Field(default="")
    final_findings: list[ReviewerFinalFindingLLM] = Field(default_factory=list)
    unresolved_sources: list[ReviewerUnresolvedSourceLLM] = Field(default_factory=list)
    contradictions: list[ReviewerContradictionLLM | str] = Field(default_factory=list)
    discarded_claims: list[ReviewerDiscardedClaimLLM | str] = Field(default_factory=list)
    recommended_order: list[str] = Field(default_factory=list)
    required_testing: list[str] = Field(default_factory=list)
    required_docs: list[str] = Field(default_factory=list)
    v1_release_criteria: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class Finding(BaseModel):
    id: str = Field(default="")
    source_finding_ids: list[str] = Field(default_factory=list)
    priority: Literal["P0", "P1", "P2", "P3"] = "P2"
    title: str = Field(default="")
    evidence: str = Field(default="")
    files: list[str] = Field(default_factory=list)
    impact: str = Field(default="")
    recommendation: str = Field(default="")
    confidence: Literal["high", "medium", "low"] = "medium"
    description: str | None = None
    source_priority: str | None = None
    reprioritization_reason: str | None = None

    @field_validator("title", "evidence", "impact", "recommendation", mode="before")
    @classmethod
    def strip_text(cls, v: Any) -> str:
        if isinstance(v, str):
            return v.strip()
        if v is None:
            return ""
        return str(v).strip()

    @field_validator("priority", mode="before")
    @classmethod
    def normalize_priority(cls, v: Any) -> str:
        if isinstance(v, str):
            v_upper = v.upper().strip()
            if v_upper in {"P0", "P1", "P2", "P3"}:
                return v_upper
            if "0" in v_upper or "BLOCKER" in v_upper or "CRITICAL" in v_upper:
                return "P0"
            if "1" in v_upper or "HIGH" in v_upper or "ALTA" in v_upper:
                return "P1"
            if "2" in v_upper or "MED" in v_upper:
                return "P2"
            if "3" in v_upper or "LOW" in v_upper or "BAJA" in v_upper:
                return "P3"
        return "P2"

    @field_validator("confidence", mode="before")
    @classmethod
    def normalize_confidence(cls, v: Any) -> str:
        if isinstance(v, str):
            v_lower = v.lower().strip()
            if v_lower in {"high", "medium", "low"}:
                return v_lower
            if v_lower in {"alta", "alto"}:
                return "high"
            if v_lower in {"media", "medio"}:
                return "medium"
            if v_lower in {"baja", "bajo"}:
                return "low"
        return "medium"

    @field_validator("files", mode="before")
    @classmethod
    def normalize_files(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            cleaned = normalize_rel_path(v)
            if cleaned and cleaned.lower() not in _PLACEHOLDERS:
                return [cleaned]
            return []
        if isinstance(v, (list, tuple, set)):
            result = []
            for item in v:
                cleaned = normalize_rel_path(str(item))
                if cleaned and cleaned.lower() not in _PLACEHOLDERS:
                    result.append(cleaned)
            return result
        return []

    @field_validator("source_finding_ids", mode="before")
    @classmethod
    def normalize_source_ids(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            if v.strip() and v.strip().lower() not in _PLACEHOLDERS:
                return [v.strip()]
            return []
        if isinstance(v, (list, tuple, set)):
            return [
                str(item).strip()
                for item in v
                if str(item).strip() and str(item).strip().lower() not in _PLACEHOLDERS
            ]
        return []

    def is_valid_finding(
        self,
        files_included: set[str] | list[str] | None = None,
        known_inventory: set[str] | list[str] | None = None,
    ) -> bool:
        """
        Returns True if finding has meaningful title, evidence, impact, recommendation
        and cited files match read files (or inventory for absence claims).
        """
        base_valid = (
            is_meaningful_text(self.title, min_len=3)
            and is_meaningful_text(self.evidence, min_len=3)
            and is_meaningful_text(self.impact, min_len=3)
            and is_meaningful_text(self.recommendation, min_len=3)
        )
        if not base_valid:
            return False

        if files_included is not None:
            inc_set = {normalize_rel_path(f) for f in files_included if f}
            inv_set = {normalize_rel_path(f) for f in known_inventory if f} if known_inventory is not None else inc_set

            for f in self.files:
                f_clean = normalize_rel_path(f)
                if not f_clean:
                    continue
                if f_clean not in inc_set:
                    # Check if finding is an explicit absence or inventory claim
                    text_blob = f"{self.title} {self.evidence} {self.impact}".lower()
                    is_absence_or_inv = any(
                        term in text_blob
                        for term in [
                            "no existe", "falta archivo", "ausencia", "inventario",
                            "tree", "not found", "missing file", "archivo faltante"
                        ]
                    )
                    if f_clean in inv_set and is_absence_or_inv:
                        continue
                    # Cited unread file as code evidence -> invalid
                    return False

        return True


class FindingDisposition(BaseModel):
    source_finding_id: str
    disposition: Literal["accepted", "merged", "rejected", "needs_verification"] = "accepted"
    final_finding_id: str | None = None
    reason: str = ""

    @field_validator("disposition", mode="before")
    @classmethod
    def normalize_disposition(cls, v: Any) -> str:
        if isinstance(v, str):
            v_l = v.lower().strip()
            if v_l in {"accepted", "merged", "rejected", "needs_verification"}:
                return v_l
            if "accept" in v_l or "aprob" in v_l or "valid" in v_l:
                return "accepted"
            if "merge" in v_l or "fusion" in v_l or "duplic" in v_l or "combin" in v_l:
                return "merged"
            if "reject" in v_l or "descart" in v_l or "rechaz" in v_l:
                return "rejected"
            if "verif" in v_l or "duda" in v_l or "pendient" in v_l:
                return "needs_verification"
        return "needs_verification"


class AgentReport(BaseModel):
    agent: str = Field(default="")
    summary: str = Field(default="")
    no_findings_reason: str | None = None
    findings: list[Finding] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    raw_output: str | None = None
    status: Literal["valid", "repaired", "fallback", "failed"] = "valid"
    retries: int = 0
    attempts: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("agent", mode="before")
    @classmethod
    def normalize_agent(cls, v: Any) -> str:
        return str(v).strip().lower() if v else ""

    @field_validator("open_questions", mode="before")
    @classmethod
    def filter_open_questions(cls, v: Any) -> list[str]:
        if isinstance(v, list):
            return [str(q).strip() for q in v if is_meaningful_text(q, min_len=5)]
        return []

    @property
    def role(self) -> str:
        return self.agent

    def ensure_finding_ids(self) -> None:
        """Assigns stable deterministic IDs to all findings."""
        role_prefix = self.agent.lower().strip() if self.agent else "spec"
        for idx, finding in enumerate(self.findings, 1):
            finding.id = f"{role_prefix}-{idx:03d}"

    def is_semantically_valid(
        self,
        files_included: set[str] | list[str] | None = None,
        known_inventory: set[str] | list[str] | None = None,
    ) -> bool:
        """Returns True if summary and findings (or no_findings_reason) are substantive."""
        if not is_meaningful_text(self.summary, min_len=10):
            return False
        if not self.findings:
            if not is_meaningful_text(self.no_findings_reason, min_len=10):
                return False
        else:
            if not all(f.is_valid_finding(files_included, known_inventory) for f in self.findings):
                return False
        return True

    def to_markdown(self) -> str:
        self.ensure_finding_ids()
        agent_name = self.agent.capitalize() if self.agent else "Especialista"
        lines = [
            f"# Reporte de Agente: {agent_name}",
            "",
            "## Resumen",
            self.summary if self.summary else "Sin resumen provisto.",
            "",
        ]

        if not self.findings:
            reason_txt = f"\n*Justificación de 0 hallazgos:* {self.no_findings_reason}" if self.no_findings_reason else ""
            lines.extend([
                f"## Hallazgos (0){reason_txt}",
                "",
                "No se detectaron hallazgos críticos.",
                "",
            ])
        else:
            lines.extend([
                f"## Hallazgos ({len(self.findings)})",
                "",
            ])
            for item in self.findings:
                files_str = ", ".join(f"`{f}`" for f in item.files) if item.files else "N/A"
                lines.extend([
                    f"### [{item.id}] [{item.priority}] {item.title}",
                    f"- **Confianza:** {item.confidence}",
                    f"- **Archivos involucrados:** {files_str}",
                    f"- **Evidencia:** {item.evidence}",
                    f"- **Impacto:** {item.impact}",
                    f"- **Recomendación:** {item.recommendation}",
                    "",
                ])

        if self.open_questions:
            lines.extend(["## Preguntas Abiertas / Decisiones Pendientes", ""])
            for q in self.open_questions:
                lines.append(f"- {q}")
            lines.append("")

        return "\n".join(lines)


@dataclass
class AccountingSummary:
    total_input_findings: int
    accepted_count: int
    merged_count: int
    rejected_count: int
    needs_verification_count: int
    accounted_count: int
    missing_ids: list[str]
    duplicate_dispositions: list[str]
    is_fully_accounted: bool


class ReviewerReport(BaseModel):
    agent: str = Field(default="reviewer")
    summary: str = Field(default="")
    v1_readiness: Literal["ready", "not_ready", "needs_verification"] = "needs_verification"
    v1_readiness_reason: str = Field(default="")
    final_findings: list[Finding] = Field(default_factory=list)
    dispositions: list[FindingDisposition] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    discarded_claims: list[str] = Field(default_factory=list)
    recommended_order: list[str] = Field(default_factory=list)
    required_testing: list[str] = Field(default_factory=list)
    required_docs: list[str] = Field(default_factory=list)
    v1_release_criteria: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    raw_output: str | None = None
    status: Literal["valid", "repaired", "fallback", "failed"] = "valid"
    retries: int = 0
    attempts: list[dict[str, Any]] = Field(default_factory=list)

    def is_valid_report(self) -> bool:
        """Returns True if the reviewer report has substantive semantic content."""
        if not is_meaningful_text(self.summary, min_len=10):
            return False
        if self.v1_readiness in {"not_ready", "needs_verification"} and not is_meaningful_text(self.v1_readiness_reason, min_len=5):
            return False
            
        return True

    @field_validator("v1_readiness", mode="before")
    @classmethod
    def normalize_readiness(cls, v: Any) -> str:
        if isinstance(v, str):
            v_l = v.lower().strip()
            if v_l in {"ready", "not_ready", "needs_verification"}:
                return v_l
            if "not" in v_l or "no" in v_l:
                return "not_ready"
            if "ready" in v_l or "list" in v_l:
                return "ready"
        return "needs_verification"

    @property
    def release_blockers(self) -> list[Finding]:
        return [f for f in self.final_findings if f.priority == "P0"]

    @property
    def p0(self) -> list[Finding]:
        return [f for f in self.final_findings if f.priority == "P0"]

    @property
    def p1(self) -> list[Finding]:
        return [f for f in self.final_findings if f.priority == "P1"]

    @property
    def p2(self) -> list[Finding]:
        return [f for f in self.final_findings if f.priority in {"P2", "P3"}]

    @property
    def deduplicated_findings(self) -> list[Finding]:
        return self.final_findings

    @property
    def executive_summary(self) -> str:
        return self.summary

    @property
    def contradictions_detected(self) -> list[str]:
        return self.contradictions

    @property
    def discarded_claims_without_evidence(self) -> list[str]:
        return self.discarded_claims

    @property
    def recommended_implementation_order(self) -> list[str]:
        return self.recommended_order

    @property
    def required_documentation(self) -> list[str]:
        return self.required_docs

    @property
    def proposed_v1_release_criteria(self) -> list[str]:
        return self.v1_release_criteria

    @property
    def open_questions_human_decisions(self) -> list[str]:
        return self.open_questions

    def to_final_markdown(
        self,
        repo_name: str,
        goal: str,
        model_name: str,
        accounting: AccountingSummary | None = None,
        specialist_statuses: dict[str, str] | None = None,
    ) -> str:
        if self.v1_readiness == "ready":
            readiness_badge = "🟢 Ready for V1"
        elif self.v1_readiness == "not_ready":
            if len(self.release_blockers) > 0:
                readiness_badge = "🔴 Not Ready for V1 (Release Blockers Present)"
            else:
                readiness_badge = "🔴 Not Ready for V1 (Essential Work Pending)"
        else:
            readiness_badge = "🟡 Needs Verification"

        lines = [
            f"# Agent Team Final Audit Report — {repo_name}",
            "",
            f"- **Objetivo:** {goal}",
            f"- **Modelo:** `{model_name}`",
            f"- **V1 Readiness:** {readiness_badge}",
            "",
        ]

        if specialist_statuses:
            lines.extend([
                "### Specialist Execution",
                "",
            ])
            for role_name, st in specialist_statuses.items():
                icon = "✓" if st in {"valid", "repaired"} else "✗"
                lines.append(f"- **{role_name.capitalize()}:** {icon} `{st}`")
            lines.append("")

        lines.extend([
            "## Executive Summary",
            self.summary if self.summary else "Sin resumen ejecutivo.",
            "",
        ])

        if self.v1_readiness_reason:
            lines.extend([
                "### V1 Readiness Assessment",
                self.v1_readiness_reason,
                "",
            ])

        # Release Blockers
        lines.extend([f"## Release Blockers ({len(self.release_blockers)})", ""])
        if self.release_blockers:
            for f in self.release_blockers:
                lines.extend(self._render_finding_block(f))
        else:
            lines.extend(["No se identificaron release blockers críticos P0.", ""])

        # Prioritized Findings P0, P1, P2
        lines.extend(["## Prioritized Findings", ""])
        
        lines.extend([f"### P0 ({len(self.p0)})", ""])
        if self.p0:
            for f in self.p0:
                lines.extend(self._render_finding_block(f))
        else:
            lines.extend(["Ninguno identificado.", ""])

        lines.extend([f"### P1 ({len(self.p1)})", ""])
        if self.p1:
            for f in self.p1:
                lines.extend(self._render_finding_block(f))
        else:
            lines.extend(["Ninguno identificado.", ""])

        lines.extend([f"### P2 ({len(self.p2)})", ""])
        if self.p2:
            for f in self.p2:
                lines.extend(self._render_finding_block(f))
        else:
            lines.extend(["Ninguno identificado.", ""])

        # Findings Accounting
        if accounting:
            lines.extend([
                "## Findings Accounting & Traceability",
                "",
                f"- **Input Specialist Findings:** {accounting.total_input_findings}",
                f"- **Final Consolidated Findings:** {len(self.final_findings)}",
                f"  - **Accepted Sources:** {accounting.accepted_count}",
                f"  - **Merged Sources:** {accounting.merged_count}",
                f"  - **Rejected Claims:** {accounting.rejected_count}",
                f"  - **Needs Verification:** {accounting.needs_verification_count}",
                f"- **Accounted for:** {accounting.accounted_count}/{accounting.total_input_findings} (100% Traceability)",
                "",
            ])

        # Rejected claims
        if self.discarded_claims:
            lines.extend(["## Rejected Claims (Sin Evidencia Suficiente)", ""])
            for d in self.discarded_claims:
                lines.append(f"- 🚫 {d}")
            lines.append("")

        # Contradictions
        if self.contradictions:
            lines.extend(["## Contradicciones Detectadas entre Agentes", ""])
            for c in self.contradictions:
                lines.append(f"- ⚠️ {c}")
            lines.append("")

        # Implementation order
        lines.extend(["## Recommended Implementation Order", ""])
        if self.recommended_order:
            for step, desc in enumerate(self.recommended_order, 1):
                lines.append(f"{step}. {desc}")
        else:
            lines.append("No se especificó un orden explícito.")
        lines.append("")

        # Required testing
        lines.extend(["## Required Testing", ""])
        if self.required_testing:
            for item in self.required_testing:
                lines.append(f"- [ ] {item}")
        else:
            lines.append("No se especificaron tests requeridos.")
        lines.append("")

        # Required documentation
        lines.extend(["## Required Documentation", ""])
        if self.required_docs:
            for item in self.required_docs:
                lines.append(f"- [ ] {item}")
        else:
            lines.append("No se especificó documentación requerida.")
        lines.append("")

        # V1 Release criteria
        lines.extend(["## Proposed V1 Release Criteria", ""])
        if self.v1_release_criteria:
            for item in self.v1_release_criteria:
                lines.append(f"- [ ] {item}")
        else:
            lines.append("No se especificaron criterios de release.")
        lines.append("")

        # Open questions
        lines.extend(["## Open Questions / Human Decisions", ""])
        if self.open_questions:
            for item in self.open_questions:
                lines.append(f"- ❓ {item}")
        else:
            lines.append("No hay preguntas abiertas pendientes.")
        lines.append("")

        return "\n".join(lines)

    def _render_finding_block(self, f: Finding) -> list[str]:
        sources_str = f" (Fuentes: {', '.join(f.source_finding_ids)})" if f.source_finding_ids else ""
        reprio_str = f" *(Re-priorizado desde {f.source_priority}: {f.reprioritization_reason})*" if f.source_priority and f.source_priority != f.priority else ""
        files_str = ", ".join(f"`{file}`" for file in f.files) if f.files else "N/A"
        return [
            f"#### [{f.id or 'REV'}] [{f.priority}] {f.title}{sources_str}{reprio_str}",
            f"- **Confianza:** {f.confidence}",
            f"- **Archivos:** {files_str}",
            f"- **Evidencia:** {f.evidence}",
            f"- **Impacto:** {f.impact}",
            f"- **Recomendación:** {f.recommendation}",
            "",
        ]


def compute_accounting_summary(
    reviewer_report: ReviewerReport,
    specialist_reports: list[AgentReport],
) -> AccountingSummary:
    """
    Computes strict deterministic accounting for all specialist finding IDs.
    Guarantees: accepted + merged + rejected + needs_verification = total_input_findings.
    """
    original_source_ids: set[str] = set()
    for s in specialist_reports:
        s_role = s.agent.lower().strip() if s.agent else "spec"
        for idx, f in enumerate(s.findings, 1):
            f.id = f"{s_role}-{idx:03d}"
            original_source_ids.add(f.id)

    total_inputs = len(original_source_ids)
    if total_inputs == 0:
        return AccountingSummary(
            total_input_findings=0,
            accepted_count=0,
            merged_count=0,
            rejected_count=0,
            needs_verification_count=0,
            accounted_count=0,
            missing_ids=[],
            duplicate_dispositions=[],
            is_fully_accounted=True,
        )

    final_by_id = {f.id: f for f in reviewer_report.final_findings if f.id and f.is_valid_finding()}

    disposition_map: dict[str, FindingDisposition] = {}
    for d in reviewer_report.dispositions:
        s_id = d.source_finding_id.strip()
        if s_id in original_source_ids:
            # Check validity of disposition
            if d.disposition in {"accepted", "merged"}:
                if not d.final_finding_id or d.final_finding_id not in final_by_id:
                    # Invalid accepted/merged without existing final finding -> needs_verification
                    d.disposition = "needs_verification"
                    d.final_finding_id = None
                    if not d.reason:
                        d.reason = "Accepted/merged source finding lacked a valid final finding; converted to needs_verification."
            elif d.disposition in {"rejected", "needs_verification"}:
                if not is_meaningful_text(d.reason):
                    d.reason = "Reviewer omitted mandatory reason for this disposition; converted automatically."
                    
            disposition_map[s_id] = d

    # Implicitly check final_findings source_finding_ids
    for f in reviewer_report.final_findings:
        if f.is_valid_finding():
            for s_id in f.source_finding_ids:
                if s_id in original_source_ids and s_id not in disposition_map:
                    disp_type = "merged" if len(f.source_finding_ids) > 1 else "accepted"
                    disposition_map[s_id] = FindingDisposition(
                        source_finding_id=s_id,
                        disposition=disp_type,
                        final_finding_id=f.id,
                    )

    missing = [s_id for s_id in sorted(original_source_ids) if s_id not in disposition_map]

    accepted = sum(1 for d in disposition_map.values() if d.disposition == "accepted")
    merged = sum(1 for d in disposition_map.values() if d.disposition == "merged")
    rejected = sum(1 for d in disposition_map.values() if d.disposition == "rejected")
    needs_verif = sum(1 for d in disposition_map.values() if d.disposition == "needs_verification")

    accounted = accepted + merged + rejected + needs_verif
    is_full = (accounted == total_inputs) and len(missing) == 0

    return AccountingSummary(
        total_input_findings=total_inputs,
        accepted_count=accepted,
        merged_count=merged,
        rejected_count=rejected,
        needs_verification_count=needs_verif,
        accounted_count=accounted,
        missing_ids=missing,
        duplicate_dispositions=[],
        is_fully_accounted=is_full,
    )


def _log_dropped_finding(run_ctx: Any, diag: dict[str, Any]) -> None:
    try:
        log_file = getattr(run_ctx, "log_file", None)
        if log_file:
            msg = f"REVIEWER FINAL FINDING DROPPED: title='{diag.get('title')}', source_ids={diag.get('source_ids')}, reason='{diag.get('reason')}'"
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
    except OSError:
        pass


def validate_and_filter_reviewer_findings(
    raw_final_findings: list[Any],
    specialist_reports: list[AgentReport],
    run_ctx: Any = None,
) -> tuple[list[Finding], list[dict[str, Any]]]:
    """
    Validates and filters Reviewer final findings based on validated specialist source evidence.
    
    Principles:
    - Specialist findings already passed Evidence-First validation with files actually read.
    - Reviewer trusts this validated evidence to consolidate.
    - Final finding's allowed files = union of files from its cited source findings.
    - Reviewer cannot introduce external files outside the union of its source findings' files.
    - If files in final finding are empty or mismatched, Python normalizes them to allowed_source_files.
    - If a final finding is dropped (e.g. empty title, zero sources, ungrounded), records diagnostic reason.
    """
    source_map: dict[str, Finding] = {}
    for s in specialist_reports:
        for f in s.findings:
            if f.id:
                source_map[f.id.lower().strip()] = f

    dropped_diagnostics: list[dict[str, Any]] = []
    valid_findings: list[Finding] = []

    for f_in in raw_final_findings:
        f_dict = f_in.model_dump() if hasattr(f_in, "model_dump") else (dict(f_in) if isinstance(f_in, dict) else f_in.__dict__)
        source_ids = f_dict.get("source_finding_ids", [])
        if isinstance(source_ids, str):
            source_ids = [source_ids]
        
        # Check source IDs exist
        valid_source_ids = [s.strip() for s in source_ids if str(s).strip().lower() in source_map]
        if not valid_source_ids:
            diag = {
                "title": f_dict.get("title", ""),
                "source_ids": source_ids,
                "reason": "unknown_or_empty_sources",
            }
            dropped_diagnostics.append(diag)
            if run_ctx:
                _log_dropped_finding(run_ctx, diag)
            continue

        # Check meaningful title, evidence, impact, recommendation
        if not is_meaningful_text(f_dict.get("title", ""), min_len=3):
            diag = {
                "title": f_dict.get("title", ""),
                "source_ids": valid_source_ids,
                "reason": "invalid_title",
            }
            dropped_diagnostics.append(diag)
            if run_ctx:
                _log_dropped_finding(run_ctx, diag)
            continue

        if not is_meaningful_text(f_dict.get("evidence", ""), min_len=3):
            diag = {
                "title": f_dict.get("title", ""),
                "source_ids": valid_source_ids,
                "reason": "invalid_evidence",
            }
            dropped_diagnostics.append(diag)
            if run_ctx:
                _log_dropped_finding(run_ctx, diag)
            continue

        # Allowed files = union of files from claimed sources
        allowed_source_files: set[str] = set()
        for sid in valid_source_ids:
            sf = source_map[sid.lower().strip()]
            for p in sf.files:
                p_clean = normalize_rel_path(p)
                if p_clean:
                    allowed_source_files.add(p_clean)

        # Normalize files
        f_files = f_dict.get("files", [])
        if isinstance(f_files, str):
            f_files = [f_files]
        normalized_files = [normalize_rel_path(p) for p in f_files if normalize_rel_path(p)]
        
        # If Reviewer files are a subset of allowed files, keep valid subset; otherwise normalize to allowed_source_files
        clean_files = [p for p in normalized_files if p in allowed_source_files]
        if not clean_files:
            clean_files = sorted(allowed_source_files)

        f_obj = Finding(
            source_finding_ids=valid_source_ids,
            priority=f_dict.get("priority", "P2"),
            source_priority=f_dict.get("source_priority"),
            reprioritization_reason=f_dict.get("reprioritization_reason"),
            title=f_dict.get("title", "").strip()[:120],
            evidence=f_dict.get("evidence", "").strip(),
            files=clean_files,
            impact=f_dict.get("impact", "").strip(),
            recommendation=f_dict.get("recommendation", "").strip(),
            confidence=f_dict.get("confidence", "medium"),
        )
        valid_findings.append(f_obj)

    return valid_findings, dropped_diagnostics


def validate_and_filter_reviewer_claims(
    raw_contradictions: list[Any],
    raw_discarded_claims: list[Any],
    original_source_finding_ids: set[str],
) -> tuple[list[str], list[str], bool]:
    """
    Validates that contradictions and discarded claims reference real source finding IDs.
    Filters out any claims mentioning non-existent IDs (e.g. hallucinated frontend-003).
    """
    had_hallucination = False
    clean_contradictions: list[str] = []
    clean_discarded: list[str] = []

    valid_id_set_lower = {sid.lower().strip() for sid in original_source_finding_ids if sid}
    id_pattern = re.compile(r"\b([a-zA-Z]+-\d{3})\b", re.IGNORECASE)

    for item in raw_contradictions:
        s_ids = getattr(item, "source_finding_ids", []) or (item.get("source_finding_ids", []) if isinstance(item, dict) else [])
        if isinstance(s_ids, str):
            s_ids = [s_ids]
        desc = getattr(item, "description", "") or (item.get("description", "") if isinstance(item, dict) else str(item))
        
        all_ids = set(s_ids) | set(id_pattern.findall(desc))
        invalid_ids = [i for i in all_ids if i.lower().strip() not in valid_id_set_lower]
        if invalid_ids:
            had_hallucination = True
            continue

        if is_meaningful_text(desc, min_len=5):
            clean_contradictions.append(desc.strip())

    for item in raw_discarded_claims:
        s_ids = getattr(item, "source_finding_ids", []) or (item.get("source_finding_ids", []) if isinstance(item, dict) else [])
        if isinstance(s_ids, str):
            s_ids = [s_ids]
        reason = getattr(item, "reason", "") or (item.get("reason", "") if isinstance(item, dict) else str(item))
        
        all_ids = set(s_ids) | set(id_pattern.findall(reason))
        invalid_ids = [i for i in all_ids if i.lower().strip() not in valid_id_set_lower]
        if invalid_ids:
            had_hallucination = True
            continue

        if is_meaningful_text(reason, min_len=5):
            clean_discarded.append(reason.strip())

    return clean_contradictions, clean_discarded, had_hallucination


def determine_deterministic_readiness(
    final_findings: list[Finding],
    accounting: AccountingSummary,
    has_failed_specialists: bool,
    reviewer_suggested_reason: str = "",
) -> tuple[Literal["ready", "not_ready", "needs_verification"], str]:
    """
    Imposes deterministic readiness consistency after all reconciliation and accounting.
    
    Rules:
    1. If any specialist failed -> needs_verification
    2. Else if final P0 > 0 -> not_ready (citing exact P0 count)
    3. Else if needs_verification sources > 0 -> needs_verification (citing exact unverified count)
    4. Else if final P1 > 0 -> not_ready (citing exact P1 count, NOT calling them P0 release blockers)
    5. Else -> ready
    """
    p0_findings = [f for f in final_findings if f.priority == "P0"]
    p1_findings = [f for f in final_findings if f.priority == "P1"]
    needs_verif_count = accounting.needs_verification_count

    if has_failed_specialists:
        return "needs_verification", "Existen especialistas fallidos en la auditoría; V1 no puede considerarse lista."

    if p0_findings:
        return "not_ready", f"Existen {len(p0_findings)} release blockers P0 sin resolver que impiden el despliegue seguro."

    if needs_verif_count > 0:
        return "needs_verification", f"Existen {needs_verif_count} hallazgos fuente que requieren verificación adicional antes del release."

    if p1_findings:
        return "not_ready", f"Existen {len(p1_findings)} hallazgos P1 esenciales para una v1 sólida que deben completarse antes del lanzamiento."

    return "ready", "No existen hallazgos bloqueantes P0 ni P1 pendientes; la base de código cumple los criterios para v1."


def deduplicate_final_findings_sources(
    final_findings: list[Finding],
    original_source_finding_ids: set[str],
) -> tuple[list[Finding], bool]:
    """
    Deduplicates source finding IDs across final findings deterministically.
    Rules:
    - A source_finding_id can only belong to ONE final finding (the first one encountered).
    - If already assigned, remove it from subsequent final findings.
    - If a final finding is left with 0 sources, discard it.
    - Re-indexes final findings as reviewer-001, reviewer-002, ...
    """
    seen_sources: set[str] = set()
    cleaned_findings: list[Finding] = []
    had_duplicate = False

    for f in final_findings:
        unique_sources = []
        for s_id in f.source_finding_ids:
            s_clean = str(s_id).strip()
            if s_clean in original_source_finding_ids:
                if s_clean in seen_sources:
                    had_duplicate = True
                else:
                    seen_sources.add(s_clean)
                    unique_sources.append(s_clean)
            else:
                had_duplicate = True
        f.source_finding_ids = unique_sources
        if unique_sources and f.is_valid_finding():
            cleaned_findings.append(f)
        else:
            had_duplicate = True

    for idx, f in enumerate(cleaned_findings, 1):
        f.id = f"reviewer-{idx:03d}"

    return cleaned_findings, had_duplicate


def reconcile_and_guarantee_accounting(
    report: ReviewerReport,
    specialist_reports: list[AgentReport],
) -> tuple[ReviewerReport, AccountingSummary]:
    """
    Guarantees that 100% of specialist findings are explicitly accounted for against
    the immutable original source finding IDs.
    
    Invariants:
    1. set(accounted_source_ids) == original_source_finding_ids.
    2. ACCEPTED requires valid non-null final_finding_id existing in final_findings.
    3. MERGED requires valid non-null final_finding_id existing in final_findings.
    4. Missing source IDs are assigned needs_verification (final_finding_id=None).
    5. Input Specialist Findings is always len(original_source_finding_ids).
    6. No final_findings == 0 with accepted > 0.
    """
    # 1. Capture immutable Ground Truth from specialist reports
    original_source_map: dict[str, Finding] = {}
    for s in specialist_reports:
        s_role = s.agent.lower().strip() if s.agent else "spec"
        for idx, f in enumerate(s.findings, 1):
            f.id = f"{s_role}-{idx:03d}"
            original_source_map[f.id] = f

    original_source_finding_ids: set[str] = set(original_source_map.keys())
    total_inputs = len(original_source_finding_ids)

    if total_inputs == 0:
        summary = compute_accounting_summary(report, specialist_reports)
        has_failed = any(s.status == "failed" for s in specialist_reports)
        new_readiness, new_reason = determine_deterministic_readiness(
            final_findings=report.final_findings,
            accounting=summary,
            has_failed_specialists=has_failed,
            reviewer_suggested_reason=report.v1_readiness_reason,
        )
        report.v1_readiness = new_readiness
        report.v1_readiness_reason = new_reason
        return report, summary

    had_recovery = False

    # 2. Filter and deduplicate source finding IDs in final_findings
    cleaned_final, had_dup_f = deduplicate_final_findings_sources(report.final_findings, original_source_finding_ids)
    if had_dup_f or len(cleaned_final) != len(report.final_findings):
        had_recovery = True

    report.final_findings = cleaned_final
    final_by_id = {f.id: f for f in report.final_findings if f.id}

    # 3. Validate and clean existing dispositions
    validated_dispositions: dict[str, FindingDisposition] = {}
    disp_prio = {"accepted": 4, "merged": 3, "needs_verification": 2, "rejected": 1}

    for d in report.dispositions:
        s_id = d.source_finding_id.strip()
        if s_id not in original_source_finding_ids:
            # Ignore dispositions referencing non-existent IDs
            continue

        # Invariant 2, 3 & 6: Validate accepted/merged disposition has a real final_finding_id
        if d.disposition in {"accepted", "merged"}:
            if not d.final_finding_id or d.final_finding_id not in final_by_id:
                # Deterministically look up by matching source_finding_ids
                matched_f = next((f for f in report.final_findings if s_id in f.source_finding_ids), None)
                if matched_f:
                    d.final_finding_id = matched_f.id
                else:
                    d.disposition = "needs_verification"
                    d.final_finding_id = None
                    d.reason = "Accepted/merged source finding lacked a valid final finding; converted to needs_verification."
                    had_recovery = True
            else:
                # Link back in final finding
                ff = final_by_id[d.final_finding_id]
                if s_id not in ff.source_finding_ids:
                    ff.source_finding_ids.append(s_id)
        elif d.disposition in {"rejected", "needs_verification"}:
            if not is_meaningful_text(d.reason):
                d.reason = "Reviewer omitted mandatory reason for this disposition; converted automatically."
                had_recovery = True

        # Handle duplicate dispositions on the same ID
        if s_id not in validated_dispositions:
            validated_dispositions[s_id] = d
        else:
            had_recovery = True
            if disp_prio.get(d.disposition, 0) > disp_prio.get(validated_dispositions[s_id].disposition, 0):
                validated_dispositions[s_id] = d

    # Also link any source finding IDs listed in final_findings that weren't in dispositions
    for f in report.final_findings:
        for s_id in f.source_finding_ids:
            if s_id in original_source_finding_ids and s_id not in validated_dispositions:
                disp_type = "merged" if len(f.source_finding_ids) > 1 else "accepted"
                validated_dispositions[s_id] = FindingDisposition(
                    source_finding_id=s_id,
                    disposition=disp_type,
                    final_finding_id=f.id,
                )

    # 4. Invariant 4: Check for missing source IDs and recover deterministically
    missing_ids = [s_id for s_id in sorted(original_source_finding_ids) if s_id not in validated_dispositions]

    if missing_ids:
        had_recovery = True
        for missing_id in missing_ids:
            source_f = original_source_map.get(missing_id)
            if not source_f:
                continue

            disc_match = any(
                disc.lower() in source_f.title.lower() or source_f.title.lower() in disc.lower()
                for disc in report.discarded_claims
                if len(disc) > 3
            )

            if disc_match:
                validated_dispositions[missing_id] = FindingDisposition(
                    source_finding_id=missing_id,
                    disposition="rejected",
                    reason=f"Coincide con afirmación descartada por falta de evidencia: {source_f.title}",
                )
            else:
                validated_dispositions[missing_id] = FindingDisposition(
                    source_finding_id=missing_id,
                    disposition="needs_verification",
                    final_finding_id=None,
                    reason="Reviewer did not account for this source finding.",
                )

    # Invariant 1: Ensure dispositions list is sorted and contains all original source IDs
    report.dispositions = [validated_dispositions[s_id] for s_id in sorted(original_source_finding_ids)]

    if had_recovery:
        report.status = "repaired"

    final_summary = compute_accounting_summary(report, specialist_reports)

    # Impose deterministic readiness consistency
    has_failed = any(s.status == "failed" for s in specialist_reports)
    new_readiness, new_reason = determine_deterministic_readiness(
        final_findings=report.final_findings,
        accounting=final_summary,
        has_failed_specialists=has_failed,
        reviewer_suggested_reason=report.v1_readiness_reason,
    )
    report.v1_readiness = new_readiness
    report.v1_readiness_reason = new_reason

    return report, final_summary


def _extract_json_candidate_strings(text: str) -> list[str]:
    """Returns possible JSON substring candidates in priority order, including repaired variants."""
    candidates = []

    # 1. Inside ```json ... ``` or ``` ... ```
    for m in re.finditer(r"```(?:json)?\s*([\{\[].*?[\}\]])\s*```", text, re.DOTALL):
        cand = m.group(1).strip()
        if cand:
            candidates.append(cand)
            rep = repair_json_string(cand)
            if rep != cand:
                candidates.append(rep)

    # 2. Outermost { ... }
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        cand = text[first_brace:last_brace + 1].strip()
        candidates.append(cand)
        rep = repair_json_string(cand)
        if rep != cand:
            candidates.append(rep)

    # 3. Outermost [ ... ]
    first_bracket = text.find("[")
    last_bracket = text.rfind("]")
    if first_bracket != -1 and last_bracket > first_bracket:
        cand = text[first_bracket:last_bracket + 1].strip()
        candidates.append(cand)
        rep = repair_json_string(cand)
        if rep != cand:
            candidates.append(rep)

    clean_text = text.strip()
    candidates.append(clean_text)
    rep_clean = repair_json_string(clean_text)
    if rep_clean != clean_text:
        candidates.append(rep_clean)

    # Deduplicate preserving order
    seen = set()
    unique_candidates = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            unique_candidates.append(c)
    return unique_candidates


def parse_agent_report(
    role: str,
    raw_text: str,
    retries: int = 0,
    files_included: set[str] | list[str] | None = None,
    known_inventory: set[str] | list[str] | None = None,
) -> tuple[AgentReport, Literal["valid", "repaired", "fallback", "failed"]]:
    """
    Robust tolerant parser for AgentReport.
    Recovers findings from JSON objects, markdown fences, JSON arrays,
    partial JSON fragments, or structured text lists, and validates against read files.
    """
    if not raw_text or not raw_text.strip():
        fallback = AgentReport(
            agent=role,
            summary="Respuesta vacía del modelo.",
            findings=[],
            open_questions=["El modelo retornó una respuesta vacía."],
            raw_output=raw_text,
            status="failed",
            retries=retries,
        )
        return fallback, "failed"

    # Step 1: Try direct parse on raw_text
    try:
        data = json.loads(raw_text.strip())
        if isinstance(data, dict):
            data["agent"] = role
            report = AgentReport.model_validate(data)
            raw_count = len(report.findings)
            valid_findings = [f for f in report.findings if f.is_valid_finding(files_included, known_inventory)]
            report.findings = valid_findings
            report.ensure_finding_ids()
            report.raw_output = raw_text
            report.retries = retries
            
            # Semantic gate
            if not report.is_semantically_valid(files_included, known_inventory):
                report.status = "failed"
                return report, "failed"

            if raw_count > 0 and len(valid_findings) < raw_count:
                report.status = "repaired"
            else:
                report.status = "valid" if retries == 0 else "repaired"
            return report, report.status
        elif isinstance(data, list):
            findings = []
            for item in data:
                if isinstance(item, dict):
                    f = Finding.model_validate(item)
                    if f.is_valid_finding(files_included, known_inventory):
                        findings.append(f)
            if findings:
                report = AgentReport(
                    agent=role,
                    summary=f"Extraídos {len(findings)} hallazgos de lista JSON.",
                    findings=findings,
                    raw_output=raw_text,
                    status="repaired",
                    retries=retries,
                )
                report.ensure_finding_ids()
                if not report.is_semantically_valid(files_included, known_inventory):
                    report.status = "failed"
                    return report, "failed"
                return report, "repaired"
    except Exception:
        pass

    # Step 2: Try JSON extraction candidates
    candidates = _extract_json_candidate_strings(raw_text)
    for cand in candidates:
        try:
            data = json.loads(cand)
            if isinstance(data, dict):
                if "findings" in data and isinstance(data["findings"], list):
                    data["agent"] = role
                    report = AgentReport.model_validate(data)
                    valid_findings = [f for f in report.findings if f.is_valid_finding(files_included, known_inventory)]
                    report.findings = valid_findings
                    report.ensure_finding_ids()
                    report.raw_output = raw_text
                    report.retries = retries
                    if not report.is_semantically_valid(files_included, known_inventory):
                        report.status = "failed"
                        return report, "failed"
                    report.status = "repaired"
                    return report, "repaired"
                elif "title" in data or "evidence" in data:
                    f = Finding.model_validate(data)
                    if f.is_valid_finding(files_included, known_inventory):
                        report = AgentReport(
                            agent=role,
                            summary=f.title,
                            findings=[f],
                            raw_output=raw_text,
                            status="repaired",
                            retries=retries,
                        )
                        report.ensure_finding_ids()
                        if not report.is_semantically_valid(files_included, known_inventory):
                            report.status = "failed"
                            return report, "failed"
                        return report, "repaired"
            elif isinstance(data, list):
                findings = []
                for item in data:
                    if isinstance(item, dict):
                        f = Finding.model_validate(item)
                        if f.is_valid_finding(files_included, known_inventory):
                            findings.append(f)
                if findings:
                    report = AgentReport(
                        agent=role,
                        summary=f"Extraídos {len(findings)} hallazgos de lista JSON.",
                        findings=findings,
                        raw_output=raw_text,
                        status="repaired",
                        retries=retries,
                    )
                    report.ensure_finding_ids()
                    if not report.is_semantically_valid(files_included, known_inventory):
                        report.status = "failed"
                        return report, "failed"
                    return report, "repaired"
        except Exception:
            continue

    # Step 3: Extract individual finding JSON blocks via regex
    extracted_findings: list[Finding] = []
    for m in re.finditer(r"\{[^{}]*?(?:title|priority|evidence)[^{}]*?\}", raw_text, re.DOTALL):
        block = m.group(0)
        try:
            item_data = json.loads(block)
            if isinstance(item_data, dict) and ("title" in item_data or "evidence" in item_data):
                f = Finding.model_validate(item_data)
                if f.is_valid_finding():
                    extracted_findings.append(f)
        except Exception:
            continue

    if extracted_findings:
        report = AgentReport(
            agent=role,
            summary=f"Recuperados {len(extracted_findings)} hallazgos mediante escaneo de fragmentos JSON.",
            findings=extracted_findings,
            raw_output=raw_text,
            status="repaired",
            retries=retries,
        )
        report.ensure_finding_ids()
        return report, "repaired"

    # Step 4: Text-based heuristic recovery from markdown bullets
    text_findings = _extract_findings_from_markdown_text(raw_text)
    if text_findings:
        valid_text_findings = [f for f in text_findings if f.is_valid_finding()]
        if valid_text_findings:
            report = AgentReport(
                agent=role,
                summary=f"Recuperados {len(valid_text_findings)} hallazgos a partir de texto estructurado.",
                findings=valid_text_findings,
                raw_output=raw_text,
                status="fallback",
                retries=retries,
            )
            report.ensure_finding_ids()
            return report, "fallback"

    # Step 5: Final degraded fallback
    first_line = raw_text.strip().split("\n")[0][:250]
    fallback_report = AgentReport(
        agent=role,
        summary=f"Respuesta no estructurada: {first_line}",
        findings=[],
        open_questions=["El modelo generó texto no estructurado sin hallazgos extraíbles."],
        raw_output=raw_text,
        status="failed",
        retries=retries,
    )
    return fallback_report, "failed"


def _extract_findings_from_markdown_text(text: str) -> list[Finding]:
    """
    Extracts findings from markdown text only when real fields are present.
    Strictly forbids fabricating evidence, impact, or recommendation placeholders.
    """
    findings = []
    # Match markdown sections like:
    # ### [P1] Title
    # - **Evidencia:** ...
    # - **Impacto:** ...
    # - **Recomendación:** ...
    blocks = re.split(r"(?:\n|^)(?:###|####|\d+\.)\s+", text)
    for block in blocks:
        if not block.strip():
            continue
        first_line = block.strip().split("\n")[0]
        p_match = re.search(r"\[?(P[0-3]|BLOCKER|CRITICAL)\]?[:\s\-]+(.+)", first_line, re.IGNORECASE)
        if not p_match:
            continue
        p_val = p_match.group(1).upper()
        title_val = p_match.group(2).strip()

        ev_match = re.search(r"(?:Evidencia|Evidence):\s*(.+)", block, re.IGNORECASE)
        imp_match = re.search(r"(?:Impacto|Impact):\s*(.+)", block, re.IGNORECASE)
        rec_match = re.search(r"(?:Recomendación|Recomendacion|Recommendation):\s*(.+)", block, re.IGNORECASE)

        ev_val = ev_match.group(1).strip() if ev_match else ""
        imp_val = imp_match.group(1).strip() if imp_match else ""
        rec_val = rec_match.group(1).strip() if rec_match else ""

        f = Finding(
            priority=p_val,
            title=title_val[:120],
            evidence=ev_val,
            impact=imp_val,
            recommendation=rec_val,
            confidence="medium",
        )
        if f.is_valid_finding():
            findings.append(f)

    return findings


def derive_dispositions_from_reviewer_output(
    final_findings: list[Finding],
    unresolved_sources: list[ReviewerUnresolvedSourceLLM] | list[dict[str, Any]],
    original_source_finding_ids: set[str],
) -> tuple[list[FindingDisposition], bool]:
    """
    Derives dispositions deterministically from final_findings and unresolved_sources.
    
    Rules:
    - final_finding with len(source_finding_ids) == 1 -> 'accepted', final_finding_id = f.id
    - final_finding with len(source_finding_ids) > 1 -> 'merged', final_finding_id = f.id
    - unresolved_sources -> 'rejected' or 'needs_verification' with reason, final_finding_id = None
    
    Validation Invariants (No Duplicate Sources):
    - A source_finding_id cannot appear in two different final_findings (deduplicated)
    - A source_finding_id cannot appear in final_findings and unresolved_sources (final_finding takes precedence)
    - A source_finding_id cannot appear twice in unresolved_sources
    """
    cleaned_final, had_duplicate = deduplicate_final_findings_sources(final_findings, original_source_finding_ids)
    disposition_map: dict[str, FindingDisposition] = {}

    # 1. Accepted / Merged derived from cleaned final_findings
    for f in cleaned_final:
        disp_type = "merged" if len(f.source_finding_ids) > 1 else "accepted"
        for s_id in f.source_finding_ids:
            disposition_map[s_id] = FindingDisposition(
                source_finding_id=s_id,
                disposition=disp_type,
                final_finding_id=f.id,
            )

    # 2. Rejected / Needs_verification derived from unresolved_sources
    for item in unresolved_sources:
        disp = getattr(item, "disposition", None) or (item.get("disposition") if isinstance(item, dict) else "rejected")
        reason = getattr(item, "reason", "") or (item.get("reason", "") if isinstance(item, dict) else "")
        source_ids = getattr(item, "source_finding_ids", []) or (item.get("source_finding_ids", []) if isinstance(item, dict) else [])
        if isinstance(source_ids, str):
            source_ids = [source_ids]

        if not is_meaningful_text(reason):
            reason = "Reviewer classified this source finding as unresolved."

        for s_id in source_ids:
            s_clean = str(s_id).strip()
            if s_clean not in original_source_finding_ids:
                continue
            if s_clean in disposition_map:
                had_duplicate = True
                continue
            disposition_map[s_clean] = FindingDisposition(
                source_finding_id=s_clean,
                disposition=disp if disp in {"rejected", "needs_verification"} else "rejected",
                reason=reason,
                final_finding_id=None,
            )

    return list(disposition_map.values()), had_duplicate


def parse_reviewer_report(
    raw_text: str,
    specialist_reports: list[AgentReport],
    retries: int = 0,
) -> tuple[ReviewerReport, Literal["valid", "repaired", "fallback", "failed"]]:
    """
    Parses ReviewerReport and executes deterministic accounting reconciliation.
    """
    parsed_report: ReviewerReport | None = None

    orig_ids: set[str] = set()
    for s in specialist_reports:
        s_role = s.agent.lower().strip() if s.agent else "spec"
        for idx, f in enumerate(s.findings, 1):
            f.id = f"{s_role}-{idx:03d}"
            orig_ids.add(f.id)

    if raw_text and raw_text.strip():
        # Try direct parse
        try:
            data = json.loads(raw_text.strip())
            if isinstance(data, dict):
                data["agent"] = "reviewer"
                raw_final = data.get("final_findings", [])
                valid_final, dropped_diags = validate_and_filter_reviewer_findings(raw_final, specialist_reports)
                for idx, f in enumerate(valid_final, 1):
                    f.id = f"reviewer-{idx:03d}"
                clean_contras, clean_discards, had_halluc = validate_and_filter_reviewer_claims(
                    data.get("contradictions", []),
                    data.get("discarded_claims", []),
                    orig_ids,
                )
                data["final_findings"] = [f.model_dump() for f in valid_final]
                data["contradictions"] = clean_contras
                data["discarded_claims"] = clean_discards
                unres_s = data.get("unresolved_sources", [])
                derived_disps, had_dup = derive_dispositions_from_reviewer_output(valid_final, unres_s, orig_ids)
                if not data.get("dispositions"):
                    data["dispositions"] = [d.model_dump() for d in derived_disps]
                parsed_report = ReviewerReport.model_validate(data)
                parsed_report.raw_output = raw_text
                had_mod = bool(retries > 0 or dropped_diags or had_halluc or had_dup)
                parsed_report.status = "valid" if not had_mod else "repaired"
                parsed_report.retries = retries
        except Exception:
            pass

        # Try candidate JSON strings
        if parsed_report is None:
            candidates = _extract_json_candidate_strings(raw_text)
            for cand in candidates:
                try:
                    data = json.loads(cand)
                    if isinstance(data, dict):
                        data["agent"] = "reviewer"
                        raw_final = data.get("final_findings", [])
                        valid_final, _ = validate_and_filter_reviewer_findings(raw_final, specialist_reports)
                        for idx, f in enumerate(valid_final, 1):
                            f.id = f"reviewer-{idx:03d}"
                        clean_contras, clean_discards, _ = validate_and_filter_reviewer_claims(
                            data.get("contradictions", []),
                            data.get("discarded_claims", []),
                            orig_ids,
                        )
                        data["final_findings"] = [f.model_dump() for f in valid_final]
                        data["contradictions"] = clean_contras
                        data["discarded_claims"] = clean_discards
                        unres_s = data.get("unresolved_sources", [])
                        derived_disps, _ = derive_dispositions_from_reviewer_output(valid_final, unres_s, orig_ids)
                        if not data.get("dispositions"):
                            data["dispositions"] = [d.model_dump() for d in derived_disps]
                        parsed_report = ReviewerReport.model_validate(data)
                        parsed_report.raw_output = raw_text
                        parsed_report.status = "repaired"
                        parsed_report.retries = retries
                        break
                except Exception:
                    continue

    if parsed_report is not None:
        reconciled, _ = reconcile_and_guarantee_accounting(parsed_report, specialist_reports)
        return reconciled, reconciled.status

    # Fallback programmatic aggregation
    open_qs = []
    for r in specialist_reports:
        open_qs.extend(r.open_questions)

    fallback = ReviewerReport(
        agent="reviewer",
        summary="Plan consolidado automáticamente a partir de los reportes especialistas.",
        v1_readiness="needs_verification",
        v1_readiness_reason="Consolidación de respaldo generada automáticamente.",
        final_findings=[],
        dispositions=[],
        contradictions=[],
        discarded_claims=[],
        recommended_order=[],
        required_testing=["Ejecutar suite de tests completa para validar hallazgos P0/P1."],
        required_docs=["Actualizar contratos y guías de inicio para v1."],
        v1_release_criteria=["Resolver todos los hallazgos confirmados P0 y validar tests."],
        open_questions=open_qs,
        raw_output=raw_text,
        status="repaired",
        retries=retries,
    )

    reconciled, _ = reconcile_and_guarantee_accounting(fallback, specialist_reports)
    if not reconciled.recommended_order:
        reconciled.recommended_order = [
            f"Resolver {item.priority}: {item.title}"
            for item in (reconciled.p0 + reconciled.p1)
        ]
    return reconciled, reconciled.status
