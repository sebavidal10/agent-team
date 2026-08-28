from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

from langchain_ollama import ChatOllama
from langgraph.graph import END, START, StateGraph

from .config import Settings
from .models import (
    AccountingSummary,
    AgentReport,
    Finding,
    FindingDisposition,
    ReviewerLLMOutput,
    ReviewerReport,
    SpecialistLLMOutput,
    is_meaningful_text,
    parse_agent_report,
    parse_reviewer_report,
    reconcile_and_guarantee_accounting,
)
from .observability import ConsoleObserver
from .repo_context import ROLE_CHAR_LIMITS, RepoSnapshot, build_global_inventory_tree, build_targeted_snapshot
from .run_manager import (
    RunContext,
    save_reviewer_output,
    save_specialist_output,
)


class TeamState(TypedDict, total=False):
    goal: str
    repo_name: str
    repo_tree: str
    repo_content: str
    architect_report: AgentReport
    backend_report: AgentReport
    frontend_report: AgentReport
    testing_report: AgentReport
    docs_report: AgentReport
    reviewer_report: ReviewerReport
    architect: str
    backend: str
    frontend: str
    testing: str
    docs: str
    reviewer: str
    role_metrics: dict[str, dict[str, Any]]


def _read_prompt(settings: Settings, name: str) -> str:
    return (settings.prompts_dir / f"{name}.md").read_text(encoding="utf-8")


def _extract_telemetry(raw_msg: Any, duration: float) -> dict[str, Any]:
    meta = {}
    if hasattr(raw_msg, "response_metadata") and isinstance(raw_msg.response_metadata, dict):
        meta = raw_msg.response_metadata
    elif isinstance(raw_msg, dict) and "response_metadata" in raw_msg:
        meta = raw_msg.get("response_metadata", {})
    return {
        "duration_seconds": round(duration, 2),
        "prompt_eval_count": meta.get("prompt_eval_count"),
        "eval_count": meta.get("eval_count"),
        "prompt_eval_duration": meta.get("prompt_eval_duration"),
        "eval_duration": meta.get("eval_duration"),
        "total_duration": meta.get("total_duration"),
    }


def build_graph(
    settings: Settings,
    snapshots: dict[str, RepoSnapshot] | RepoSnapshot,
    run_ctx: RunContext | None = None,
    observer: ConsoleObserver | None = None,
):
    llm = ChatOllama(
        model=settings.model,
        base_url=settings.base_url,
        temperature=0.1,
        num_ctx=settings.num_ctx,
    )

    if isinstance(snapshots, RepoSnapshot):
        snapshot_map = {
            "architect": snapshots,
            "backend": snapshots,
            "frontend": snapshots,
            "testing": snapshots,
            "docs": snapshots,
            "reviewer": snapshots,
        }
    else:
        snapshot_map = snapshots

    def _format_repo_context(role: str) -> str:
        snap = snapshot_map.get(role) or snapshot_map.get("architect")
        if not snap:
            return ""
        return f"""REPOSITORIO: {snap.root.name}

ÁRBOL DE ARCHIVOS RELEVANTES ({role.upper()}):
{snap.tree}

CONTENIDO SELECCIONADO:
{snap.content}
"""

    def call_specialist(role: str):
        base_role_prompt = _read_prompt(settings, role)
        shared_rubric = _read_prompt(settings, "_shared_rubric")
        system_prompt = f"{base_role_prompt}\n\n---\n{shared_rubric}" if shared_rubric else base_role_prompt

        snap = snapshot_map.get(role)
        file_count = len(snap.files_included) if snap else 0
        total_chars = snap.total_chars if snap else 0
        discarded_files = snap.candidates_discarded if snap else 0
        files_inc = set(snap.files_included) if snap else set()
        all_inv = set(snap.tree.splitlines()) if snap else files_inc

        structured_llm = llm.with_structured_output(SpecialistLLMOutput, include_raw=True)

        def node(state: TeamState):
            t0 = time.time()
            if observer:
                observer.start_agent(
                    role=role,
                    files_count=file_count,
                    context_chars=total_chars,
                    model=settings.model,
                    discarded_files=discarded_files,
                )

            report: AgentReport | None = None
            raw_text = ""
            retries = 0
            attempts_list: list[dict[str, Any]] = []

            try:
                # Phase 1: Context Preparation
                if observer:
                    observer.phase_start(role, "Preparing context...")
                repo_context = _format_repo_context(role)
                if observer:
                    observer.phase_done(role, "Preparing context")

                # Phase 2: Calling Ollama with native structured output
                if observer:
                    observer.phase_start(role, f"Calling Ollama ({settings.model})...")

                messages = [
                    ("system", system_prompt),
                    ("human", f"""
OBJETIVO DEL USUARIO:
{state['goal']}

{repo_context}

Analiza con base en evidencia disponible y genera el reporte estructurado en JSON.
Si no existen hallazgos de severidad P0/P1/P2 sustentados con evidencia, usa 'findings': [] y explica en 'no_findings_reason' qué se auditó.
"""),
                ]

                try:
                    att_t0 = time.time()
                    result = structured_llm.invoke(messages)
                    att_dur = time.time() - att_t0
                    raw_msg = result.get("raw") if isinstance(result, dict) else None
                    raw_text = raw_msg.content if hasattr(raw_msg, "content") else (str(raw_msg) if raw_msg else "")
                    parsed = result.get("parsed") if isinstance(result, dict) else (result if isinstance(result, SpecialistLLMOutput) else None)
                    parsing_err = result.get("parsing_error") if isinstance(result, dict) else None

                    att_meta = _extract_telemetry(raw_msg, att_dur)
                    att_meta.update({
                        "attempt": 1,
                        "type": "primary_structured",
                        "raw_output": raw_text,
                        "parsing_error": str(parsing_err) if parsing_err else None,
                    })
                    attempts_list.append(att_meta)

                    if isinstance(parsed, SpecialistLLMOutput):
                        findings_conv = [Finding(**f.model_dump()) for f in parsed.findings]
                        raw_findings_count = len(findings_conv)
                        valid_findings = [f for f in findings_conv if f.is_valid_finding(files_inc, all_inv)]

                        # Check zero-findings reason validity
                        zero_findings_valid = (
                            raw_findings_count == 0
                            and is_meaningful_text(parsed.no_findings_reason, min_len=10)
                        )

                        if zero_findings_valid or (raw_findings_count > 0 and len(valid_findings) == raw_findings_count):
                            report = AgentReport(
                                agent=role,
                                summary=parsed.summary,
                                no_findings_reason=parsed.no_findings_reason,
                                findings=valid_findings,
                                open_questions=parsed.open_questions,
                                raw_output=raw_text,
                                status="valid",
                                retries=0,
                                attempts=attempts_list,
                            )
                            report.ensure_finding_ids()
                            if observer:
                                observer.phase_done(role, "Ollama response received")
                                observer.phase_done(role, "Structured output valid (schema + semantic quality)")
                        elif valid_findings:
                            report = AgentReport(
                                agent=role,
                                summary=parsed.summary,
                                no_findings_reason=parsed.no_findings_reason,
                                findings=valid_findings,
                                open_questions=parsed.open_questions,
                                raw_output=raw_text,
                                status="repaired",
                                retries=0,
                                attempts=attempts_list,
                            )
                            report.ensure_finding_ids()
                            if observer:
                                observer.phase_done(role, "Ollama response received")
                                observer.phase_done(role, "Structured output repaired (invalid findings filtered)")
                        else:
                            # Semantic failure: findings invalid or zero findings without substantive reason
                            if observer:
                                observer.phase_done(role, "Ollama response received")
                                observer.agent_warning(role, "findings semantically invalid (missing evidence or missing no_findings_reason)\n            analysis retry 1/1")
                            
                            retries = 1
                            feedback_msg = (
                                "Tus hallazgos anteriores fueron descartados porque carecían de evidencia concreta de archivos leídos, "
                                "o se omitieron findings sin proveer un 'no_findings_reason' sustantivo. "
                                "Vuelve a auditar el contexto del repositorio. Si el código no presenta problemas sustentables, devuelve 'findings': [] "
                                "y explica detalladamente en 'no_findings_reason' qué se revisó."
                            )
                            retry_messages = list(messages) + [
                                ("assistant", raw_text),
                                ("human", feedback_msg),
                            ]
                            try:
                                r_t0 = time.time()
                                retry_res = structured_llm.invoke(retry_messages)
                                r_dur = time.time() - r_t0
                                r_raw_msg = retry_res.get("raw") if isinstance(retry_res, dict) else None
                                r_raw_text = r_raw_msg.content if hasattr(r_raw_msg, "content") else (str(r_raw_msg) if r_raw_msg else raw_text)
                                r_parsed = retry_res.get("parsed") if isinstance(retry_res, dict) else None
                                r_err = retry_res.get("parsing_error") if isinstance(retry_res, dict) else None

                                r_meta = _extract_telemetry(r_raw_msg, r_dur)
                                r_meta.update({
                                    "attempt": 2,
                                    "type": "semantic_analysis_retry",
                                    "raw_output": r_raw_text,
                                    "parsing_error": str(r_err) if r_err else None,
                                })
                                attempts_list.append(r_meta)

                                if isinstance(r_parsed, SpecialistLLMOutput):
                                    r_findings = [Finding(**f.model_dump()) for f in r_parsed.findings]
                                    r_valid = [f for f in r_findings if f.is_valid_finding(files_inc, all_inv)]
                                    report = AgentReport(
                                        agent=role,
                                        summary=r_parsed.summary,
                                        no_findings_reason=r_parsed.no_findings_reason,
                                        findings=r_valid,
                                        open_questions=r_parsed.open_questions,
                                        raw_output=r_raw_text,
                                        status="repaired" if r_valid or (len(r_findings) == 0 and is_meaningful_text(r_parsed.no_findings_reason, min_len=10)) else "failed",
                                        retries=1,
                                        attempts=attempts_list,
                                    )
                                    report.ensure_finding_ids()
                                    if observer:
                                        observer.phase_done(role, f"Analysis retry completed ({len(r_valid)} valid findings)")
                            except Exception:
                                pass
                    else:
                        # Schema / JSON parsing failure
                        if observer:
                            observer.phase_done(role, "Ollama response received")
                            observer.agent_warning(role, f"structured schema invalid ({parsing_err})\n            format recovery 1/1")

                except Exception as structured_err:
                    if observer:
                        observer.phase_done(role, "Ollama response received")
                        observer.agent_warning(role, f"structured output error ({structured_err})\n            format recovery 1/1")

                # Format recovery if schema/parsing failed (no full re-analysis)
                if report is None and raw_text:
                    retries = 1
                    report, status_flag = parse_agent_report(role, raw_text, retries=1, files_included=files_inc, known_inventory=all_inv)
                    report.agent = role
                    report.ensure_finding_ids()
                    report.raw_output = raw_text
                    report.retries = 1
                    report.attempts = attempts_list
                    if observer:
                        if status_flag in {"valid", "repaired"}:
                            observer.phase_done(role, f"Structured output {status_flag} from raw text")
                        else:
                            observer.agent_warning(role, "Structured output degraded to fallback")

                if report is None:
                    # Final degraded fallback
                    report = AgentReport(
                        agent=role,
                        summary="No se pudo extraer reporte válido.",
                        findings=[],
                        open_questions=["Fallo en la estructuración de la respuesta."],
                        status="failed",
                        retries=retries,
                        raw_output=raw_text,
                        attempts=attempts_list,
                    )
                    report.ensure_finding_ids()

                # Mandatory Final Semantic Gate for Specialists
                if not report.is_semantically_valid(files_inc, all_inv):
                    report.status = "failed"

                report.agent = role
                report.retries = retries
                report.attempts = attempts_list
                report.ensure_finding_ids()

                # Phase 4: Saving report
                if run_ctx:
                    save_specialist_output(run_ctx, role, report)
                    if observer:
                        observer.phase_done(role, "Report saved")

                duration = time.time() - t0

                if observer:
                    observer.complete_agent(
                        role=role,
                        report=report,
                        duration=duration,
                        files_count=file_count,
                        context_chars=total_chars,
                        discarded_files=discarded_files,
                    )

            except Exception as err:
                duration = time.time() - t0
                if observer:
                    observer.agent_failed(role, str(err), duration)
                report = AgentReport(
                    agent=role,
                    summary=f"Fallo durante ejecución: {err}",
                    findings=[],
                    open_questions=[f"Error de ejecución: {err}"],
                    status="failed",
                    retries=retries,
                    raw_output=raw_text or str(err),
                    attempts=attempts_list,
                )
                report.ensure_finding_ids()
                raw_text = str(err)
                if run_ctx:
                    save_specialist_output(run_ctx, role, report)

            metrics = dict(state.get("role_metrics", {}))
            metrics[role] = {
                "files": file_count,
                "context_chars": total_chars,
                "findings": len(report.findings) if report else 0,
                "duration_seconds": round(duration, 1),
                "structured_output": report.status if report else "failed",
                "retries": report.retries if report else 0,
                "discarded_files": discarded_files,
                "files_list": snap.files_included if snap else [],
                "attempts": attempts_list,
            }

            return {
                f"{role}_report": report,
                role: raw_text,
                "role_metrics": metrics,
            }

        return node

    def call_reviewer():
        base_reviewer_prompt = _read_prompt(settings, "reviewer")
        shared_rubric = _read_prompt(settings, "_shared_rubric")
        system_prompt = f"{base_reviewer_prompt}\n\n---\n{shared_rubric}" if shared_rubric else base_reviewer_prompt

        structured_reviewer_llm = llm.with_structured_output(ReviewerLLMOutput, include_raw=True)

        def node(state: TeamState):
            t0 = time.time()

            specialists = [
                state.get("architect_report"),
                state.get("backend_report"),
                state.get("frontend_report"),
                state.get("testing_report"),
                state.get("docs_report"),
            ]
            valid_specialists = [r for r in specialists if r is not None]
            for s in valid_specialists:
                s.ensure_finding_ids()

            specialist_statuses = {s.agent: s.status for s in valid_specialists}
            has_failed_specialists = any(s.status == "failed" for s in valid_specialists)

            all_input_findings: list[Finding] = [f for s in valid_specialists for f in s.findings]
            total_input_count = len(all_input_findings)

            # Build targeted evidence context for Reviewer with global repo inventory
            target_files = set()
            for s in valid_specialists:
                for f in s.findings:
                    for p in f.files:
                        if p and p.strip() and p.lower() not in {"n/a", "none", "unknown", "null"}:
                            target_files.add(p.strip())

            arch_snap = snapshot_map.get("architect")
            root_dir = arch_snap.root if arch_snap else Path(".")
            global_inv_tree = build_global_inventory_tree(root_dir)
            global_inv_set = set(global_inv_tree.splitlines())

            targeted_snap = build_targeted_snapshot(
                root=root_dir,
                target_files=target_files,
                full_inventory_tree=global_inv_tree,
                max_file_chars=settings.max_file_chars,
                max_total_chars=ROLE_CHAR_LIMITS.get("reviewer", 25_000),
            )

            file_count = len(targeted_snap.files_included)
            total_chars = targeted_snap.total_chars
            discarded_files = targeted_snap.candidates_discarded
            targeted_files_set = set(targeted_snap.files_included)

            if observer:
                observer.start_agent(
                    role="reviewer",
                    files_count=file_count,
                    context_chars=total_chars,
                    model=settings.model,
                    discarded_files=discarded_files,
                )

            report: ReviewerReport | None = None
            accounting_summary = None
            raw_text = ""
            retries = 0
            attempts_list: list[dict[str, Any]] = []

            try:
                # Phase 1: Aggregate specialist reports with IDs and status
                if observer:
                    observer.phase_start("reviewer", f"Aggregating {total_input_count} specialist findings")

                repo_context = f"""REPOSITORIO: {targeted_snap.root.name}

ÁRBOL COMPLETO DE ARCHIVOS (INVENTARIO):
{targeted_snap.tree}

CONTENIDO DE ARCHIVOS CITADOS COMO EVIDENCIA POR ESPECIALISTAS ({file_count} archivos):
{targeted_snap.content if targeted_snap.content else "No se citaron archivos de código específicos."}
"""

                reports_summary = [
                    f"### REPORTE AGENTE: {r.agent.upper()} (Estado: {r.status})\n{r.to_markdown()}\n"
                    for r in valid_specialists
                ]
                full_specialists_context = "\n\n".join(reports_summary)

                findings_id_list = "\n".join([
                    f"- `{f.id}` [{f.priority}] {f.title} (Archivos: {', '.join(f.files) if f.files else 'N/A'})"
                    for f in all_input_findings
                ])

                if observer:
                    observer.phase_done("reviewer", f"{total_input_count} specialist findings aggregated")

                # Phase 2: Calling Ollama
                if observer:
                    observer.phase_start("reviewer", f"Calling Ollama ({settings.model})...")

                messages = [
                    ("system", system_prompt),
                    ("human", f"""
OBJETIVO DEL USUARIO:
{state['goal']}

{repo_context}

ESTADO DE EJECUCIÓN DE ESPECIALISTAS:
{chr(10).join(f"- {role.capitalize()}: {st}" for role, st in specialist_statuses.items())}

LISTADO EXACTO DE HALLAZGOS A AUDITAR (Total: {total_input_count}):
{findings_id_list}

REPORTES DETALLADOS DE CADA ESPECIALISTA:
{full_specialists_context}

INSTRUCCIÓN TECH LEAD:
Debes clasificar y dar disposición en 'dispositions' a CADA UNO de los {total_input_count} IDs de entrada ('accepted', 'merged', 'rejected', 'needs_verification') y construir 'final_findings' con 'source_finding_ids'. Genera el JSON completo con resumen ejecutivo y evaluación justificada de v1 readiness.
"""),
                ]

                try:
                    att_t0 = time.time()
                    result = structured_reviewer_llm.invoke(messages)
                    att_dur = time.time() - att_t0

                    raw_msg = result.get("raw") if isinstance(result, dict) else None
                    raw_text = raw_msg.content if hasattr(raw_msg, "content") else (str(raw_msg) if raw_msg else "")
                    parsed = result.get("parsed") if isinstance(result, dict) else (result if isinstance(result, ReviewerLLMOutput) else None)
                    parsing_err = result.get("parsing_error") if isinstance(result, dict) else None

                    att_meta = _extract_telemetry(raw_msg, att_dur)
                    att_meta.update({
                        "attempt": 1,
                        "type": "primary_review",
                        "raw_output": raw_text,
                        "parsing_error": str(parsing_err) if parsing_err else None,
                    })
                    attempts_list.append(att_meta)

                    if isinstance(parsed, ReviewerLLMOutput):
                        # Convert DTO to ReviewerReport and validate final findings against targeted context
                        conv_findings = [
                            Finding(**f.model_dump()) for f in parsed.final_findings
                            if Finding(**f.model_dump()).is_valid_finding(targeted_files_set, global_inv_set)
                        ]
                        conv_dispositions = [FindingDisposition(**d.model_dump()) for d in parsed.dispositions]
                        report = ReviewerReport(
                            agent="reviewer",
                            summary=parsed.summary,
                            v1_readiness=parsed.v1_readiness,
                            v1_readiness_reason=parsed.v1_readiness_reason,
                            final_findings=conv_findings,
                            dispositions=conv_dispositions,
                            contradictions=parsed.contradictions,
                            discarded_claims=parsed.discarded_claims,
                            recommended_order=parsed.recommended_order,
                            required_testing=parsed.required_testing,
                            required_docs=parsed.required_docs,
                            v1_release_criteria=parsed.v1_release_criteria,
                            open_questions=parsed.open_questions,
                            raw_output=raw_text,
                            status="valid",
                            retries=0,
                            attempts=attempts_list,
                        )
                        report, accounting_summary = reconcile_and_guarantee_accounting(report, valid_specialists)

                        # Enforce that audit with failed specialists cannot be READY
                        if has_failed_specialists and report.v1_readiness == "ready":
                            report.v1_readiness = "needs_verification"
                            if "fallidos" not in report.v1_readiness_reason.lower():
                                report.v1_readiness_reason = (report.v1_readiness_reason + " [Existen especialistas fallidos en la auditoría; V1 no puede considerarse lista]").strip()

                        # Check semantic quality
                        if not report.is_valid_report():
                            if observer:
                                observer.phase_done("reviewer", "Ollama response received")
                                observer.agent_warning("reviewer", "Reviewer output lacks substantive evaluation\n            review retry 1/1")
                            
                            retries = 1
                            retry_messages = list(messages) + [
                                ("assistant", raw_text),
                                ("human", (
                                    "Tu reporte anterior carecía de un análisis ejecutivo sustantivo o evaluación de readiness justificada. "
                                    "Por favor consolida los hallazgos de los especialistas con un análisis técnico sustantivo y juicio claro sobre cada hallazgo."
                                )),
                            ]
                            try:
                                r_t0 = time.time()
                                retry_res = structured_reviewer_llm.invoke(retry_messages)
                                r_dur = time.time() - r_t0

                                r_raw_msg = retry_res.get("raw") if isinstance(retry_res, dict) else None
                                r_raw_text = r_raw_msg.content if hasattr(r_raw_msg, "content") else (str(r_raw_msg) if r_raw_msg else raw_text)
                                r_parsed = retry_res.get("parsed") if isinstance(retry_res, dict) else None
                                r_err = retry_res.get("parsing_error") if isinstance(retry_res, dict) else None

                                r_meta = _extract_telemetry(r_raw_msg, r_dur)
                                r_meta.update({
                                    "attempt": 2,
                                    "type": "semantic_review_retry",
                                    "raw_output": r_raw_text,
                                    "parsing_error": str(r_err) if r_err else None,
                                })
                                attempts_list.append(r_meta)

                                if isinstance(r_parsed, ReviewerLLMOutput):
                                    r_conv_f = [
                                        Finding(**f.model_dump()) for f in r_parsed.final_findings
                                        if Finding(**f.model_dump()).is_valid_finding(targeted_files_set, global_inv_set)
                                    ]
                                    r_conv_d = [FindingDisposition(**d.model_dump()) for d in r_parsed.dispositions]
                                    report = ReviewerReport(
                                        agent="reviewer",
                                        summary=r_parsed.summary,
                                        v1_readiness=r_parsed.v1_readiness,
                                        v1_readiness_reason=r_parsed.v1_readiness_reason,
                                        final_findings=r_conv_f,
                                        dispositions=r_conv_d,
                                        contradictions=r_parsed.contradictions,
                                        discarded_claims=r_parsed.discarded_claims,
                                        recommended_order=r_parsed.recommended_order,
                                        required_testing=r_parsed.required_testing,
                                        required_docs=r_parsed.required_docs,
                                        v1_release_criteria=r_parsed.v1_release_criteria,
                                        open_questions=r_parsed.open_questions,
                                        raw_output=r_raw_text,
                                        status="repaired",
                                        retries=1,
                                        attempts=attempts_list,
                                    )
                                    report, accounting_summary = reconcile_and_guarantee_accounting(report, valid_specialists)
                                    if has_failed_specialists and report.v1_readiness == "ready":
                                        report.v1_readiness = "needs_verification"
                            except Exception:
                                pass

                        if observer:
                            observer.phase_done("reviewer", "Ollama response received")
                            if report.status == "valid" and report.is_valid_report():
                                observer.phase_done("reviewer", f"Structured final plan valid ({accounting_summary.accounted_count}/{total_input_count} accounted)")
                            else:
                                observer.agent_warning("reviewer", f"Reviewer output repaired ({accounting_summary.accounted_count}/{total_input_count} accounted)")

                except Exception as structured_err:
                    if observer:
                        observer.phase_done("reviewer", "Ollama response received")
                        observer.agent_warning("reviewer", f"structured reviewer output invalid ({structured_err})\n            retry 1/1")

                # If primary structured call failed, parse from raw_text or fallback
                if report is None:
                    retries = 1
                    report, status_flag = parse_reviewer_report(raw_text, valid_specialists, retries=1)
                    report.raw_output = raw_text
                    report.retries = 1
                    report.attempts = attempts_list
                    report, accounting_summary = reconcile_and_guarantee_accounting(report, valid_specialists)
                    if has_failed_specialists and report.v1_readiness == "ready":
                        report.v1_readiness = "needs_verification"
                    if observer:
                        observer.phase_done("reviewer", f"Structured final plan {status_flag} ({accounting_summary.accounted_count}/{total_input_count} accounted)")

                report.retries = retries
                report.attempts = attempts_list
                if accounting_summary is None:
                    report, accounting_summary = reconcile_and_guarantee_accounting(report, valid_specialists)
                    if has_failed_specialists and report.v1_readiness == "ready":
                        report.v1_readiness = "needs_verification"

                # MANDATORY FINAL REVIEWER GATE
                if not report.is_valid_report():
                    report.status = "failed"

                # Phase 4: Saving report
                if run_ctx and report is not None:
                    save_reviewer_output(
                        run_ctx,
                        report,
                        repo_name=state.get("repo_name", "repository"),
                        goal=state.get("goal", ""),
                        model_name=settings.model,
                        accounting=accounting_summary,
                        specialist_statuses=specialist_statuses,
                    )
                    if observer:
                        observer.phase_done("reviewer", "Final report saved")

                duration = time.time() - t0

                if observer and report is not None:
                    observer.complete_agent(
                        role="reviewer",
                        report=report,
                        duration=duration,
                        files_count=file_count,
                        context_chars=total_chars,
                        discarded_files=discarded_files,
                    )

            except Exception as err:
                duration = time.time() - t0
                if observer:
                    observer.agent_failed("reviewer", str(err), duration)
                report, _ = parse_reviewer_report("", valid_specialists, retries=retries)
                report.raw_output = raw_text or str(err)
                report.retries = retries
                report.attempts = attempts_list
                report, accounting_summary = reconcile_and_guarantee_accounting(report, valid_specialists)
                if has_failed_specialists and report.v1_readiness == "ready":
                    report.v1_readiness = "needs_verification"
                raw_text = str(err)
                if run_ctx:
                    save_reviewer_output(
                        run_ctx,
                        report,
                        repo_name=state.get("repo_name", "repository"),
                        goal=state.get("goal", ""),
                        model_name=settings.model,
                        accounting=accounting_summary,
                        specialist_statuses=specialist_statuses,
                    )

            metrics = dict(state.get("role_metrics", {}))
            metrics["reviewer"] = {
                "files": file_count,
                "context_chars": total_chars,
                "findings": len(report.final_findings) if report else 0,
                "duration_seconds": round(duration, 1),
                "structured_output": report.status if report else "failed",
                "retries": report.retries if report else 0,
                "discarded_files": discarded_files,
                "files_list": targeted_snap.files_included if targeted_snap else [],
                "attempts": attempts_list,
            }

            return {
                "reviewer_report": report,
                "reviewer": raw_text,
                "role_metrics": metrics,
            }

        return node

    architect = call_specialist("architect")
    backend = call_specialist("backend")
    frontend = call_specialist("frontend")
    testing = call_specialist("testing")
    docs = call_specialist("docs")

    reviewer = call_reviewer()

    graph = StateGraph(TeamState)
    graph.add_node("architect", architect)
    graph.add_node("backend", backend)
    graph.add_node("frontend", frontend)
    graph.add_node("testing", testing)
    graph.add_node("docs", docs)
    graph.add_node("reviewer", reviewer)

    graph.add_edge(START, "architect")
    graph.add_edge("architect", "backend")
    graph.add_edge("backend", "frontend")
    graph.add_edge("frontend", "testing")
    graph.add_edge("testing", "docs")
    graph.add_edge("docs", "reviewer")
    return graph.compile()

