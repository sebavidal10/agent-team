import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_team.models import (
    AccountingSummary,
    AgentReport,
    Finding,
    FindingDisposition,
    ReviewerReport,
    compute_accounting_summary,
    is_meaningful_text,
    parse_agent_report,
    parse_reviewer_report,
    reconcile_and_guarantee_accounting,
)
from agent_team.observability import Colors, ConsoleObserver
from agent_team.repo_context import RepoSnapshot
from agent_team.run_manager import (
    init_run,
    save_context_files,
    save_manifest,
    save_reviewer_output,
    save_specialist_output,
)


class TestModelsAndRun(unittest.TestCase):
    def test_finding_schema_and_normalization(self):
        f = Finding(
            id="backend-001",
            priority="critical",
            title="Database leak",
            evidence="db.py:20",
            files="src/db.py",
            impact="Data loss",
            recommendation="Close connection",
            confidence="ALTA",
        )
        self.assertEqual(f.id, "backend-001")
        self.assertEqual(f.priority, "P0")
        self.assertEqual(f.confidence, "high")
        self.assertEqual(f.files, ["src/db.py"])
        self.assertTrue(f.is_valid_finding())

    def test_case_1_accounting_completo(self):
        """CASO 1 — 15 source findings con 5 accepted, 5 merged, 3 rejected, 2 needs_verification."""
        specialist_reports = []
        for r in ["architect", "backend", "frontend", "testing", "docs"]:
            findings = [
                Finding(priority="P1", title=f"Issue {r}-1", evidence=f"{r}.py:10", impact="Risk 1", recommendation="Fix 1"),
                Finding(priority="P1", title=f"Issue {r}-2", evidence=f"{r}.py:20", impact="Risk 2", recommendation="Fix 2"),
                Finding(priority="P2", title=f"Issue {r}-3", evidence=f"{r}.py:30", impact="Risk 3", recommendation="Fix 3"),
            ]
            rep = AgentReport(agent=r, findings=findings)
            rep.ensure_finding_ids()
            specialist_reports.append(rep)

        all_ids = [f.id for r in specialist_reports for f in r.findings]
        self.assertEqual(len(all_ids), 15)

        # Build reviewer dispositions
        disps = []
        # 5 accepted
        for i in range(5):
            disps.append(FindingDisposition(source_finding_id=all_ids[i], disposition="accepted", final_finding_id=f"rev-{i+1}"))
        # 5 merged
        for i in range(5, 10):
            disps.append(FindingDisposition(source_finding_id=all_ids[i], disposition="merged", final_finding_id="rev-merged-1"))
        # 3 rejected
        for i in range(10, 13):
            disps.append(FindingDisposition(source_finding_id=all_ids[i], disposition="rejected", reason="No evidence"))
        # 2 needs_verification
        for i in range(13, 15):
            disps.append(FindingDisposition(source_finding_id=all_ids[i], disposition="needs_verification", reason="Requires tests"))

        rev = ReviewerReport(
            summary="Full accounting test",
            dispositions=disps,
            final_findings=[
                Finding(id=f"rev-{i+1}", source_finding_ids=[all_ids[i]], priority="P1", title=f"Final {i+1}", evidence=f"file-{i}.py:10", impact="Impact risk", recommendation="Fix recommendation")
                for i in range(5)
            ] + [
                Finding(id="rev-merged-1", source_finding_ids=all_ids[5:10], priority="P1", title="Merged issue", evidence="merged.py:10", impact="Merged impact", recommendation="Fix merged")
            ],
        )

        acc = compute_accounting_summary(rev, specialist_reports)
        self.assertEqual(acc.total_input_findings, 15)
        self.assertEqual(acc.accepted_count, 5)
        self.assertEqual(acc.merged_count, 5)
        self.assertEqual(acc.rejected_count, 3)
        self.assertEqual(acc.needs_verification_count, 2)
        self.assertEqual(acc.accounted_count, 15)
        self.assertEqual(len(acc.missing_ids), 0)
        self.assertTrue(acc.is_fully_accounted)

    def test_case_2_finding_desaparecido_auto_recovery(self):
        """CASO 2 — 15 input findings, Reviewer solo referencia 14 -> detectado y recuperado como needs_verification."""
        specialist_reports = []
        for r in ["architect", "backend", "frontend", "testing", "docs"]:
            findings = [
                Finding(priority="P1", title=f"Issue {r}-1", evidence=f"{r}.py:10", impact="Risk", recommendation="Fix"),
                Finding(priority="P1", title=f"Issue {r}-2", evidence=f"{r}.py:20", impact="Risk", recommendation="Fix"),
                Finding(priority="P2", title=f"Issue {r}-3", evidence=f"{r}.py:30", impact="Risk", recommendation="Fix"),
            ]
            rep = AgentReport(agent=r, findings=findings)
            rep.ensure_finding_ids()
            specialist_reports.append(rep)

        all_ids = [f.id for r in specialist_reports for f in r.findings]
        # Only account for 14
        disps = [FindingDisposition(source_finding_id=s_id, disposition="accepted", final_finding_id=f"rev-{i}") for i, s_id in enumerate(all_ids[:14])]
        final_findings = [Finding(id=f"rev-{i}", source_finding_ids=[s_id], priority="P1", title=f"Final {i}", evidence="file.py:10", impact="Impact risk", recommendation="Fix recommendation") for i, s_id in enumerate(all_ids[:14])]
        rev = ReviewerReport(summary="Incomplete report", dispositions=disps, final_findings=final_findings)

        reconciled, acc = reconcile_and_guarantee_accounting(rev, specialist_reports)
        self.assertEqual(acc.accounted_count, 15)
        self.assertEqual(acc.accepted_count, 14)
        self.assertEqual(acc.needs_verification_count, 1)
        self.assertEqual(acc.missing_ids, [])
        self.assertTrue(acc.is_fully_accounted)
        self.assertIn(all_ids[14], [d.source_finding_id for d in reconciled.dispositions])

    def test_case_3_id_inexistente(self):
        """CASO 3 — Reviewer referencia backend-999 que no existe -> accounting solo valida IDs reales."""
        rep = AgentReport(agent="backend", findings=[Finding(priority="P0", title="Real issue", evidence="b.py:10", impact="Risk", recommendation="Fix")])
        rep.ensure_finding_ids()

        rev = ReviewerReport(
            summary="Test invalid ID",
            dispositions=[
                FindingDisposition(source_finding_id="backend-001", disposition="accepted", final_finding_id="rev-001"),
                FindingDisposition(source_finding_id="backend-999", disposition="accepted", final_finding_id="rev-001"),
            ],
            final_findings=[Finding(id="rev-001", source_finding_ids=["backend-001"], priority="P0", title="Real", evidence="b.py:10", impact="Risk", recommendation="Fix")],
        )

        acc = compute_accounting_summary(rev, [rep])
        self.assertEqual(acc.total_input_findings, 1)
        self.assertEqual(acc.accounted_count, 1)
        self.assertTrue(acc.is_fully_accounted)

    def test_case_4_duplicate_accounting_invalido(self):
        """CASO 4 — Mismo source ID marcado accepted y rejected -> reconciliado determinísticamente."""
        rep = AgentReport(agent="docs", findings=[Finding(priority="P0", title="Docs issue", evidence="d.md:10", impact="Risk", recommendation="Fix")])
        rep.ensure_finding_ids()

        rev = ReviewerReport(
            summary="Conflicting dispositions",
            dispositions=[
                FindingDisposition(source_finding_id="docs-001", disposition="accepted", final_finding_id="rev-001"),
                FindingDisposition(source_finding_id="docs-001", disposition="rejected", reason="Rejected note"),
            ],
            final_findings=[Finding(id="rev-001", source_finding_ids=["docs-001"], priority="P0", title="Docs", evidence="d.md:10", impact="Risk", recommendation="Fix")],
        )

        reconciled, acc = reconcile_and_guarantee_accounting(rev, [rep])
        self.assertEqual(acc.accounted_count, 1)
        self.assertEqual(len(reconciled.dispositions), 1)
        self.assertEqual(reconciled.dispositions[0].disposition, "accepted")

    def test_case_5_merge_accounting(self):
        """CASO 5 — backend-001 y testing-002 se fusionan en reviewer-003 -> ambos accounted."""
        b_rep = AgentReport(agent="backend", findings=[Finding(priority="P0", title="Endpoint missing validation", evidence="api.py:10", impact="Risk", recommendation="Fix")])
        t_rep = AgentReport(agent="testing", findings=[Finding(priority="P1", title="Missing test for invalid payload", evidence="test_api.py:10", impact="Risk", recommendation="Fix")])
        b_rep.ensure_finding_ids()
        t_rep.ensure_finding_ids()

        rev = ReviewerReport(
            summary="Merged plan",
            final_findings=[
                Finding(
                    id="reviewer-003",
                    source_finding_ids=["backend-001", "testing-001"],
                    priority="P0",
                    title="Unified validation and test coverage",
                    evidence="api.py:30 and test_api.py",
                    impact="Risk of unhandled errors",
                    recommendation="Add tests and validation",
                )
            ],
            dispositions=[
                FindingDisposition(source_finding_id="backend-001", disposition="merged", final_finding_id="reviewer-003"),
                FindingDisposition(source_finding_id="testing-001", disposition="merged", final_finding_id="reviewer-003"),
            ],
        )

        acc = compute_accounting_summary(rev, [b_rep, t_rep])
        self.assertEqual(acc.total_input_findings, 2)
        self.assertEqual(acc.merged_count, 2)
        self.assertEqual(acc.accounted_count, 2)
        self.assertTrue(acc.is_fully_accounted)

    def test_case_6_repaired_malformed_finding_rejected_or_verification(self):
        """CASO 6 — Finding malformado con solo description sin title/evidence no se promueve a 'Hallazgo sin título'."""
        f_bad = Finding(description="Generic pentesting text with no title or evidence")
        self.assertFalse(f_bad.is_valid_finding())

        rev = ReviewerReport(
            summary="Test malformed",
            final_findings=[f_bad],
            dispositions=[],
        )

        spec = AgentReport(agent="backend", findings=[Finding(priority="P1", title="Valid issue", evidence="b.py:10", impact="Risk", recommendation="Fix")])
        spec.ensure_finding_ids()

        reconciled, acc = reconcile_and_guarantee_accounting(rev, [spec])
        # Malformed finding with empty title was stripped from final_findings
        for f in reconciled.final_findings:
            self.assertNotEqual(f.title, "Hallazgo sin título")
            self.assertNotEqual(f.title, "")
            self.assertTrue(f.is_valid_finding())

    def test_case_7_reprioritization_with_reason(self):
        """CASO 7 — Docs marcó P0 (falta README setup), Tech Lead corrige a P1 con razón."""
        doc_f = Finding(id="docs-001", priority="P0", title="README missing startup steps", evidence="README.md:1", impact="Dificulta onboarding", recommendation="Añadir pasos a README")
        doc_rep = AgentReport(agent="docs", findings=[doc_f])

        rev_f = Finding(
            id="reviewer-002",
            source_finding_ids=["docs-001"],
            priority="P1",
            source_priority="P0",
            reprioritization_reason="No bloquea el arranque en producción; requisito de onboarding v1.",
            title="README missing startup steps",
            evidence="README.md:1",
            impact="Dificulta onboarding",
            recommendation="Añadir pasos a README",
            confidence="high",
        )

        rev = ReviewerReport(
            summary="Reprioritized report",
            final_findings=[rev_f],
            dispositions=[
                FindingDisposition(source_finding_id="docs-001", disposition="accepted", final_finding_id="reviewer-002", reason="Reprioritized to P1")
            ],
        )

        reconciled, acc = reconcile_and_guarantee_accounting(rev, [doc_rep])
        self.assertEqual(len(reconciled.p0), 0)
        self.assertEqual(len(reconciled.p1), 1)
        self.assertEqual(reconciled.p1[0].source_priority, "P0")
        self.assertIn("No bloquea el arranque", reconciled.p1[0].reprioritization_reason)

        final_md = reconciled.to_final_markdown("demo", "goal", "qwen", acc)
        self.assertIn("Re-priorizado desde P0", final_md)

    def test_case_8_priority_counts_in_observability(self):
        """CASO 8 — Final findings P0=2, P1=2, P2=1 -> Observability Reviewer muestra 2/2/1, NO 0/0/0."""
        findings = [
            Finding(id="rev-1", priority="P0", title="Issue 1", evidence="e1.py:10", impact="Risk", recommendation="Fix"),
            Finding(id="rev-2", priority="P0", title="Issue 2", evidence="e2.py:10", impact="Risk", recommendation="Fix"),
            Finding(id="rev-3", priority="P1", title="Issue 3", evidence="e3.py:10", impact="Risk", recommendation="Fix"),
            Finding(id="rev-4", priority="P1", title="Issue 4", evidence="e4.py:10", impact="Risk", recommendation="Fix"),
            Finding(id="rev-5", priority="P2", title="Issue 5", evidence="e5.py:10", impact="Risk", recommendation="Fix"),
        ]
        rev = ReviewerReport(summary="Review summary", final_findings=findings)

        colors = Colors(enabled=False)
        observer = ConsoleObserver(colors=colors, is_tty=False)
        observer.complete_agent("reviewer", rev, duration=24.0, files_count=36, context_chars=51500)

        row = observer.rows["reviewer"]
        self.assertEqual(row.findings_count, 5)
        self.assertEqual(row.p0, 2)
        self.assertEqual(row.p1, 2)
        self.assertEqual(row.p2, 1)

    def test_case_9_all_16_specialist_findings_preserved(self):
        """CASO 9 — Architect(3), Backend(3), Frontend(3), Testing(3), Docs(4) = 16 findings preserved."""
        roles_counts = [("architect", 3), ("backend", 3), ("frontend", 3), ("testing", 3), ("docs", 4)]
        specs = []
        for role, count in roles_counts:
            findings = [Finding(priority="P1", title=f"{role} issue {i}", evidence=f"{role}.py:10", impact="Risk", recommendation="Fix") for i in range(count)]
            rep = AgentReport(agent=role, findings=findings)
            rep.ensure_finding_ids()
            specs.append(rep)

        all_input_ids = [f.id for s in specs for f in s.findings]
        self.assertEqual(len(all_input_ids), 16)
        self.assertEqual(len(set(all_input_ids)), 16)

        # Empty reviewer report -> auto-reconciliation preserves all 16 in dispositions as needs_verification without inventing final findings
        empty_rev = ReviewerReport(summary="Empty model output")
        reconciled, acc = reconcile_and_guarantee_accounting(empty_rev, specs)

        self.assertEqual(acc.total_input_findings, 16)
        self.assertEqual(acc.accounted_count, 16)
        self.assertEqual(acc.needs_verification_count, 16)
        self.assertEqual(len(reconciled.dispositions), 16)
        self.assertTrue(acc.is_fully_accounted)

    def test_case_10_reviewer_fails_accounting_after_retry(self):
        """CASO 10 — Reviewer falla accounting y omite 3 hallazgos -> se asignan needs_verification automáticamente."""
        spec = AgentReport(
            agent="testing",
            findings=[
                Finding(id="testing-001", priority="P0", title="Flaky suite", evidence="t.py:10", impact="Risk", recommendation="Fix"),
                Finding(id="testing-002", priority="P1", title="Missing coverage", evidence="t2.py:10", impact="Risk", recommendation="Fix"),
                Finding(id="testing-003", priority="P2", title="Slow test", evidence="t3.py:10", impact="Risk", recommendation="Fix"),
            ],
        )
        rev = ReviewerReport(
            summary="Reviewer only answered testing-001",
            final_findings=[Finding(id="rev-001", source_finding_ids=["testing-001"], priority="P0", title="Flaky suite", evidence="t.py:10", impact="Risk", recommendation="Fix")],
            dispositions=[FindingDisposition(source_finding_id="testing-001", disposition="accepted", final_finding_id="rev-001")],
        )

        reconciled, acc = reconcile_and_guarantee_accounting(rev, [spec])
        self.assertEqual(acc.accounted_count, 3)
        self.assertEqual(acc.accepted_count, 1)
        self.assertEqual(acc.needs_verification_count, 2)
        self.assertEqual(reconciled.status, "repaired")

    def test_case_11_empty_specialists_valid_zero(self):
        """CASO 11 — Si no existen hallazgos especialistas, 0 findings es perfectamente válido sin warnings falsos."""
        empty_specs = [AgentReport(agent="architect", findings=[]), AgentReport(agent="backend", findings=[])]
        rev = ReviewerReport(summary="Everything is clean and green.")
        reconciled, acc = reconcile_and_guarantee_accounting(rev, empty_specs)

        self.assertEqual(acc.total_input_findings, 0)
        self.assertEqual(acc.accounted_count, 0)
        self.assertEqual(len(reconciled.final_findings), 0)
        self.assertTrue(acc.is_fully_accounted)

    def test_case_12_contradictions_preserved_with_accounting(self):
        """CASO 12 — Incompatibilidades entre especialistas registradas en contradictions sin perder accounting."""
        b_rep = AgentReport(agent="backend", findings=[Finding(id="backend-001", priority="P0", title="Auth validated on route", evidence="auth.py:10", impact="Risk", recommendation="Fix")])
        t_rep = AgentReport(agent="testing", findings=[Finding(id="testing-001", priority="P0", title="Route accepts unauthenticated request", evidence="test_auth.py:10", impact="Risk", recommendation="Fix")])

        rev = ReviewerReport(
            summary="Contradiction detected",
            contradictions=["backend-001 y testing-001 contradicen si la ruta de auth valida requests."],
            dispositions=[
                FindingDisposition(source_finding_id="backend-001", disposition="needs_verification", reason="Contradicción con testing-001"),
                FindingDisposition(source_finding_id="testing-001", disposition="needs_verification", reason="Contradicción con backend-001"),
            ],
            final_findings=[
                Finding(id="rev-001", source_finding_ids=["backend-001", "testing-001"], priority="P0", title="Verificar validación de auth", evidence="auth.py y test_auth.py", impact="Risk", recommendation="Fix")
            ],
        )

        acc = compute_accounting_summary(rev, [b_rep, t_rep])
        self.assertEqual(acc.total_input_findings, 2)
        self.assertEqual(acc.needs_verification_count, 2)
        self.assertEqual(acc.accounted_count, 2)
        self.assertEqual(len(rev.contradictions), 1)

    def test_run_directory_structure_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_out = Path(tmpdir)
            run_ctx = init_run(base_out, timestamp="20260827-180000")

            self.assertTrue(run_ctx.run_dir.exists())
            self.assertTrue(run_ctx.context_dir.exists())
            self.assertTrue(run_ctx.reports_dir.exists())
            self.assertTrue(run_ctx.markdown_dir.exists())

            # Test logging
            run_ctx.log("Test log entry")
            self.assertTrue(run_ctx.log_file.exists())
            self.assertIn("Test log entry", run_ctx.log_file.read_text(encoding="utf-8"))

            # Test context files
            dummy_snap = RepoSnapshot(
                root=Path("/fake/repo"),
                tree="src/main.py",
                content="print('hello')",
                files_included=["src/main.py", "README.md"],
                total_chars=50,
                candidates_total=5,
                candidates_discarded=3,
            )
            save_context_files(run_ctx, {"architect": dummy_snap, "backend": dummy_snap})
            self.assertTrue((run_ctx.context_dir / "architect-files.txt").exists())
            self.assertTrue((run_ctx.context_dir / "backend-files.txt").exists())

            # Test specialist output saving
            spec_report = AgentReport(
                agent="architect",
                summary="Arch summary",
                findings=[Finding(priority="P0", title="Arch 1", evidence="e.py")],
            )
            spec_report.ensure_finding_ids()
            save_specialist_output(run_ctx, "architect", spec_report)
            self.assertTrue((run_ctx.reports_dir / "architect.json").exists())
            self.assertTrue((run_ctx.markdown_dir / "architect.md").exists())

            # Test reviewer output saving
            reviewer_report = ReviewerReport(
                summary="All clear",
                final_findings=[Finding(id="reviewer-001", priority="P0", title="Blocker", evidence="b.py")],
            )
            acc = AccountingSummary(1, 1, 0, 0, 0, 1, [], [], True)
            save_reviewer_output(
                run_ctx,
                reviewer_report,
                repo_name="demo",
                goal="Audit",
                model_name="test-model",
                accounting=acc,
            )
            self.assertTrue((run_ctx.reports_dir / "reviewer.json").exists())
            self.assertTrue((run_ctx.markdown_dir / "reviewer.md").exists())
            self.assertTrue(run_ctx.final_report_file.exists())

            # Test manifest saving
            role_metrics = {
                "architect": {
                    "files": 2,
                    "context_chars": 50,
                    "findings": 1,
                    "duration_seconds": 1.2,
                    "structured_output": "valid",
                    "retries": 0,
                    "files_list": ["src/main.py", "README.md"],
                }
            }
            save_manifest(
                run_ctx=run_ctx,
                repo_name="demo",
                repo_path="/fake/repo",
                goal="Audit",
                model="test-model",
                role_metrics=role_metrics,
                reviewer_report=reviewer_report,
                accounting=acc,
            )
            self.assertTrue(run_ctx.manifest_file.exists())
            manifest_data = json.loads(run_ctx.manifest_file.read_text(encoding="utf-8"))
            self.assertEqual(manifest_data["repo_name"], "demo")
    def test_reviewer_node_execution_and_symbol_resolution(self):
        """Verify that the reviewer node in graph.py executes its accounting reconciliation without any NameError."""
        from unittest.mock import MagicMock, patch
        from agent_team.config import load_settings
        from agent_team.graph import build_graph

        settings = load_settings()
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_repo = Path(tmpdir)
            src_dir = temp_repo / "src"
            src_dir.mkdir(parents=True, exist_ok=True)
            main_file = src_dir / "main.py"
            main_file.write_text("print('hello')", encoding="utf-8")

            dummy_snap = RepoSnapshot(
                root=temp_repo,
                tree="src/main.py",
                content="print('hello')",
                files_included=["src/main.py"],
                total_chars=50,
                candidates_total=1,
                candidates_discarded=0,
            )

            with patch("agent_team.graph.ChatOllama") as mock_ollama_cls:
                mock_llm_instance = MagicMock()
                mock_structured_llm = MagicMock()

                def mock_structured_side_effect(messages):
                    from agent_team.models import ReviewerFinalFindingLLM, ReviewerLLMOutput, SpecialistFindingLLM, SpecialistLLMOutput
                    msg_str = str(messages)
                    if "reviewer" in msg_str.lower() or "tech lead" in msg_str.lower():
                        return ReviewerLLMOutput(
                            summary="Reviewer plan sustantivo para la versión 1.0.",
                            v1_readiness="ready",
                            v1_readiness_reason="Todo listo para producción.",
                            final_findings=[
                                ReviewerFinalFindingLLM(
                                    source_finding_ids=["architect-001"],
                                    priority="P0",
                                    title="Arch issue",
                                    evidence="src/main.py:10",
                                    files=["src/main.py"],
                                    impact="Risk",
                                    recommendation="Fix",
                                )
                            ],
                            unresolved_sources=[],
                        )
                    return SpecialistLLMOutput(
                        summary="Specialist summary sustantivo del código auditado.",
                        findings=[
                            SpecialistFindingLLM(
                                priority="P0",
                                title="Arch issue",
                                evidence="src/main.py:10",
                                files=["src/main.py"],
                                impact="Risk",
                                recommendation="Fix",
                            )
                        ],
                    )

                mock_structured_llm.invoke.side_effect = mock_structured_side_effect
                mock_llm_instance.with_structured_output.return_value = mock_structured_llm
                mock_ollama_cls.return_value = mock_llm_instance

                compiled_graph = build_graph(
                    settings=settings,
                    snapshots={"architect": dummy_snap, "backend": dummy_snap, "frontend": dummy_snap, "testing": dummy_snap, "docs": dummy_snap, "reviewer": dummy_snap},
                )

                state = {
                    "goal": "Test goal",
                    "repo_name": "repo",
                    "role_metrics": {},
                }
                out = compiled_graph.invoke(state)
                self.assertIn("reviewer_report", out)
                rev_out = out["reviewer_report"]
                self.assertIsNotNone(rev_out)
                self.assertEqual(rev_out.agent, "reviewer")
                self.assertEqual(len(rev_out.final_findings), 1)
                self.assertEqual(rev_out.final_findings[0].id, "reviewer-001")

    def test_regression_real_run_15_inputs_6_returned(self):
        """TEST DE REGRESIÓN: 15 source IDs, Reviewer solo devuelve 6 dispositions -> 9 missing detectados y recuperados."""
        specialist_reports = []
        for role in ["architect", "backend", "frontend", "testing", "docs"]:
            findings = [
                Finding(priority="P1", title=f"{role} issue 1", evidence=f"{role}.py:10", impact="Risk", recommendation="Fix"),
                Finding(priority="P1", title=f"{role} issue 2", evidence=f"{role}.py:20", impact="Risk", recommendation="Fix"),
                Finding(priority="P2", title=f"{role} issue 3", evidence=f"{role}.py:30", impact="Risk", recommendation="Fix"),
            ]
            rep = AgentReport(agent=role, findings=findings)
            rep.ensure_finding_ids()
            specialist_reports.append(rep)

        original_ids = [f.id for s in specialist_reports for f in s.findings]
        self.assertEqual(len(original_ids), 15)

        # Reviewer only outputs dispositions for architect (3) and docs (3)
        partial_disps = [
            FindingDisposition(source_finding_id="architect-001", disposition="accepted", final_finding_id="rev-001"),
            FindingDisposition(source_finding_id="architect-002", disposition="accepted", final_finding_id="rev-001"),
            FindingDisposition(source_finding_id="architect-003", disposition="rejected", reason="No evidence"),
            FindingDisposition(source_finding_id="docs-001", disposition="accepted", final_finding_id="rev-002"),
            FindingDisposition(source_finding_id="docs-002", disposition="accepted", final_finding_id="rev-002"),
            FindingDisposition(source_finding_id="docs-003", disposition="accepted", final_finding_id="rev-002"),
        ]
        rev = ReviewerReport(
            summary="Partial reviewer output",
            final_findings=[
                Finding(id="rev-001", source_finding_ids=["architect-001", "architect-002"], priority="P0", title="Arch blocker", evidence="a.py:10", impact="Risk", recommendation="Fix"),
                Finding(id="rev-002", source_finding_ids=["docs-001", "docs-002", "docs-003"], priority="P1", title="Docs issue", evidence="d.md:10", impact="Risk", recommendation="Fix"),
            ],
            dispositions=partial_disps,
        )

        reconciled, acc = reconcile_and_guarantee_accounting(rev, specialist_reports)

        # 1. Total inputs must be 15
        self.assertEqual(acc.total_input_findings, 15)
        # 2. Accounted count must be exactly 15
        self.assertEqual(acc.accounted_count, 15)
        self.assertEqual(acc.missing_ids, [])
        self.assertTrue(acc.is_fully_accounted)

        # 3. Exactly 9 missing IDs must be converted to needs_verification
        disposition_map = {d.source_finding_id: d for d in reconciled.dispositions}
        self.assertEqual(len(disposition_map), 15)

        omitted_prefixes = ["backend", "frontend", "testing"]
        for prefix in omitted_prefixes:
            for idx in range(1, 4):
                s_id = f"{prefix}-{idx:03d}"
                self.assertIn(s_id, disposition_map)
                disp = disposition_map[s_id]
                self.assertEqual(disp.disposition, "needs_verification")
                self.assertIsNone(disp.final_finding_id)
                self.assertEqual(disp.reason, "Reviewer did not account for this source finding.")

    def test_accepted_without_final_finding_is_invalid(self):
        """TEST ACCEPTED SIN FINAL: accepted + final_finding_id=null es inválido y se convierte a needs_verification."""
        doc_rep = AgentReport(
            agent="docs",
            findings=[
                Finding(priority="P0", title="Doc 1", evidence="d.md:10", impact="Risk", recommendation="Fix"),
                Finding(priority="P1", title="Doc 2", evidence="d.md:20", impact="Risk", recommendation="Fix"),
            ],
        )
        doc_rep.ensure_finding_ids()

        # Reviewer output has final_findings = [] but marks docs-001 as accepted with final_finding_id = None
        rev = ReviewerReport(
            summary="Invalid reviewer output",
            final_findings=[],
            dispositions=[
                FindingDisposition(source_finding_id="docs-001", disposition="accepted", final_finding_id=None),
                FindingDisposition(source_finding_id="docs-002", disposition="accepted", final_finding_id="non-existent-id"),
            ],
        )

        reconciled, acc = reconcile_and_guarantee_accounting(rev, [doc_rep])

        for d in reconciled.dispositions:
            self.assertNotEqual(d.disposition, "accepted")
            self.assertEqual(d.disposition, "needs_verification")
            self.assertIsNone(d.final_finding_id)
            self.assertIn("lacked a valid final finding", d.reason)

        self.assertEqual(acc.accepted_count, 0)
        self.assertEqual(acc.needs_verification_count, 2)
        self.assertEqual(acc.total_input_findings, 2)
        self.assertEqual(acc.accounted_count, 2)

    def test_final_report_shows_original_input_count(self):
        """TEST FINAL REPORT: final-report.md muestra Input Specialist Findings: 15 (NUNCA 6)."""
        specialist_reports = []
        for role in ["architect", "backend", "frontend", "testing", "docs"]:
            findings = [Finding(priority="P1", title=f"{role} {i}", evidence=f"{role}.py:10", impact="Risk", recommendation="Fix") for i in range(3)]
            rep = AgentReport(agent=role, findings=findings)
            rep.ensure_finding_ids()
            specialist_reports.append(rep)

        # Reviewer returns only 6 dispositions initially
        rev = ReviewerReport(
            summary="Reviewer plan",
            dispositions=[
                FindingDisposition(source_finding_id="architect-001", disposition="rejected", reason="None"),
            ],
        )
        reconciled, acc = reconcile_and_guarantee_accounting(rev, specialist_reports)
        final_md = reconciled.to_final_markdown("test-repo", "Test goal", "test-model", acc)

        self.assertIn("- **Input Specialist Findings:** 15", final_md)
        self.assertNotIn("- **Input Specialist Findings:** 6\n", final_md)
        self.assertNotIn("- **Input Specialist Findings:** 1\n", final_md)
        self.assertIn("- **Accounted for:** 15/15 (100% Traceability)", final_md)

    def test_backend_frontend_testing_ids_never_lost(self):
        """TEST BACKEND/FRONTEND/TESTING: Assert explícito de que ningún ID de rol puede desaparecer."""
        specialist_reports = []
        for role in ["architect", "backend", "frontend", "testing", "docs"]:
            findings = [Finding(priority="P1", title=f"{role} {i}", evidence=f"{role}.py:10", impact="Risk", recommendation="Fix") for i in range(3)]
            rep = AgentReport(agent=role, findings=findings)
            rep.ensure_finding_ids()
            specialist_reports.append(rep)

        # Empty reviewer report
        rev = ReviewerReport(summary="Empty")
        reconciled, acc = reconcile_and_guarantee_accounting(rev, specialist_reports)

        accounted_ids = {d.source_finding_id for d in reconciled.dispositions}
        for role in ["architect", "backend", "frontend", "testing", "docs"]:
            for i in range(1, 4):
                expected_id = f"{role}-{i:03d}"
                self.assertIn(expected_id, accounted_ids)


    def test_semantic_validation_title_empty_or_whitespace_or_na(self):
        """1. title="" -> invalid, 2. title="   " -> invalid, 3. title="N/A" -> invalid."""
        f1 = Finding(priority="P1", title="", evidence="file.py:10", impact="Risk", recommendation="Fix")
        self.assertFalse(f1.is_valid_finding())

        f2 = Finding(priority="P1", title="   ", evidence="file.py:10", impact="Risk", recommendation="Fix")
        self.assertFalse(f2.is_valid_finding())

        f3 = Finding(priority="P1", title="N/A", evidence="file.py:10", impact="Risk", recommendation="Fix")
        self.assertFalse(f3.is_valid_finding())

        f4 = Finding(priority="P1", title="n/a", evidence="file.py:10", impact="Risk", recommendation="Fix")
        self.assertFalse(f4.is_valid_finding())

    def test_semantic_validation_evidence_empty_or_placeholder(self):
        """4. evidence="" -> invalid, and evidence='none' / 'sin información' -> invalid."""
        f1 = Finding(priority="P1", title="Valid title", evidence="", impact="Risk", recommendation="Fix")
        self.assertFalse(f1.is_valid_finding())

        f2 = Finding(priority="P1", title="Valid title", evidence="   ", impact="Risk", recommendation="Fix")
        self.assertFalse(f2.is_valid_finding())

        f3 = Finding(priority="P1", title="Valid title", evidence="sin información", impact="Risk", recommendation="Fix")
        self.assertFalse(f3.is_valid_finding())

        f4 = Finding(priority="P1", title="Valid title", evidence="none", impact="Risk", recommendation="Fix")
        self.assertFalse(f4.is_valid_finding())

    def test_semantic_validation_impact_empty_or_placeholder(self):
        """5. impact="" -> invalid, and impact='N/A' -> invalid."""
        f1 = Finding(priority="P1", title="Valid title", evidence="src/db.py:20", impact="", recommendation="Fix")
        self.assertFalse(f1.is_valid_finding())

        f2 = Finding(priority="P1", title="Valid title", evidence="src/db.py:20", impact="N/A", recommendation="Fix")
        self.assertFalse(f2.is_valid_finding())

    def test_semantic_validation_recommendation_empty_or_placeholder(self):
        """6. recommendation="" -> invalid, and recommendation='unknown' -> invalid."""
        f1 = Finding(priority="P1", title="Valid title", evidence="src/db.py:20", impact="Risk", recommendation="")
        self.assertFalse(f1.is_valid_finding())

        f2 = Finding(priority="P1", title="Valid title", evidence="src/db.py:20", impact="Risk", recommendation="unknown")
        self.assertFalse(f2.is_valid_finding())

    def test_semantic_validation_complete_finding_valid(self):
        """7. finding completo -> valid."""
        f = Finding(
            priority="P1",
            title="Missing token authorization on user deletion",
            evidence="src/controllers/user.ts: deleteUser does not check jwt roles",
            files=["src/controllers/user.ts"],
            impact="Users may delete records outside their tenant.",
            recommendation="Validate role claim before issuing database query.",
            confidence="high",
        )
        self.assertTrue(f.is_valid_finding())

    def test_findings_empty_list_is_valid_agent_report(self):
        """8. findings=[] -> AgentReport válido."""
        rep = AgentReport(
            agent="backend",
            summary="Se revisaron los endpoints y no se detectaron vulnerabilidades ni inconsistencias.",
            findings=[],
            open_questions=[],
        )
        self.assertEqual(len(rep.findings), 0)
        self.assertEqual(rep.status, "valid")

    def test_semantic_failure_cannot_mark_structured_output_valid(self):
        """9. semantic failure no puede marcar structured_output=valid."""
        raw_json = json.dumps({
            "agent": "backend",
            "summary": "Analisis backend",
            "findings": [
                {
                    "priority": "P1",
                    "title": "",
                    "evidence": "",
                    "files": [],
                    "impact": "",
                    "recommendation": "",
                    "description": "N/A",
                }
            ],
            "open_questions": [],
        })
        rep, status = parse_agent_report("backend", raw_json)
        self.assertNotEqual(status, "valid")
        self.assertEqual(len(rep.findings), 0)

    def test_retry_recovers_and_drops_invalid_findings_without_losing_valid_ones(self):
        """10. retry puede corregir finding inválido y preservar los válidos del mismo reporte."""
        raw_json = json.dumps({
            "agent": "backend",
            "summary": "Analisis con 1 valido y 1 invalido",
            "findings": [
                {
                    "priority": "P0",
                    "title": "SQL Injection in search query",
                    "evidence": "src/search.py: raw query concatenation",
                    "files": ["src/search.py"],
                    "impact": "Data exfiltration",
                    "recommendation": "Use parameterized queries",
                    "confidence": "high",
                },
                {
                    "priority": "P1",
                    "title": "N/A",
                    "evidence": "N/A",
                    "files": [],
                    "impact": "N/A",
                    "recommendation": "N/A",
                },
            ],
            "open_questions": [],
        })
        rep, status = parse_agent_report("backend", raw_json)
        self.assertEqual(status, "repaired")
        self.assertEqual(len(rep.findings), 1)
        self.assertEqual(rep.findings[0].title, "SQL Injection in search query")

    def test_recovery_does_not_invent_fake_fields(self):
        """11. recovery no inventa title/evidence/impact/recommendation."""
        raw_text = "Texto no estructurado con basura sin JSON y sin headings de prioridad."
        rep, status = parse_agent_report("frontend", raw_text)
        self.assertEqual(len(rep.findings), 0)
        for f in rep.findings:
            self.assertNotEqual(f.title, "Hallazgo sin título")

    def test_specialists_have_independent_prompts_and_no_sequential_leak(self):
        """12. Frontend/Backend/Architect usan prompts separados y 13. un especialista no recibe accidentalmente el output completo del anterior."""
        from unittest.mock import MagicMock, patch
        from agent_team.config import load_settings
        from agent_team.graph import build_graph
        from agent_team.repo_context import RepoSnapshot

        settings = load_settings()
        snap = RepoSnapshot(
            root=Path("/fake/repo"),
            tree="src/main.py",
            content="print('hello')",
            files_included=["src/main.py"],
            total_chars=50,
            candidates_total=1,
            candidates_discarded=0,
        )

        with patch("agent_team.graph.ChatOllama") as mock_ollama_cls:
            mock_llm_instance = MagicMock()
            mock_structured_llm = MagicMock()

            captured_prompts: dict[str, list[Any]] = {}

            def mock_structured_side_effect(messages):
                msg_str = str(messages)
                if "reviewer" in msg_str.lower() or "tech lead" in msg_str.lower():
                    return ReviewerReport(
                        agent="reviewer",
                        summary="Consolidated",
                        final_findings=[],
                        dispositions=[],
                    )
                # Capture specialist prompt
                for role in ["architect", "backend", "frontend", "testing", "docs"]:
                    if f'agent": "{role}"' in msg_str.lower() or f"agente {role}" in msg_str.lower() or f"# {role.capitalize()}" in msg_str:
                        captured_prompts[role] = messages
                return AgentReport(
                    agent="specialist",
                    summary="Clean report",
                    findings=[],
                )

            mock_structured_llm.invoke.side_effect = mock_structured_side_effect
            mock_llm_instance.with_structured_output.return_value = mock_structured_llm
            mock_ollama_cls.return_value = mock_llm_instance

            compiled_graph = build_graph(
                settings=settings,
                snapshots={"architect": snap, "backend": snap, "frontend": snap, "testing": snap, "docs": snap, "reviewer": snap},
            )

            state = {
                "goal": "Test goal",
                "repo_name": "repo",
                "role_metrics": {},
                "architect_report": AgentReport(agent="architect", summary="SECRET_ARCHITECT_TEXT_NOT_LEAKED"),
            }

            out = compiled_graph.invoke(state)
            self.assertIn("reviewer_report", out)

            # Check that backend prompt does NOT contain SECRET_ARCHITECT_TEXT_NOT_LEAKED
            for role, msgs in captured_prompts.items():
                if role != "reviewer":
                    for role_type, text in msgs:
                        self.assertNotIn("SECRET_ARCHITECT_TEXT_NOT_LEAKED", text)
                        self.assertNotIn("PLAN ARQUITECTO:", text)


    def test_case_13_agent_identity_spoofing_prevented(self):
        """CASO A y B - Identity spoofing (e.g. LLM outputs agent='code reviewer' instead of 'backend') is prevented at parsing."""
        raw_json_a = json.dumps({
            "agent": "code reviewer",
            "summary": "Analisis backend valid",
            "findings": [{"priority": "P1", "title": "Valid title A", "evidence": "Valid evidence A", "files": [], "impact": "Valid impact A", "recommendation": "Valid recommendation A"}],
        })
        rep_a, status_a = parse_agent_report("backend", raw_json_a)
        self.assertEqual(rep_a.agent, "backend")
        self.assertEqual(rep_a.findings[0].id, "backend-001")

        raw_json_b = json.dumps({
            "agent": "automated testing",
            "summary": "Analisis frontend valid",
            "findings": [{"priority": "P1", "title": "Valid title B", "evidence": "Valid evidence B", "files": [], "impact": "Valid impact B", "recommendation": "Valid recommendation B"}],
        })
        rep_b, status_b = parse_agent_report("frontend", raw_json_b)
        self.assertEqual(rep_b.agent, "frontend")
        self.assertEqual(rep_b.findings[0].id, "frontend-001")

    def test_case_14_reviewer_semantic_validity_no_lists_required(self):
        """CASO C, F y G - Semantic validity of ReviewerReport without forcing arbitrary lists."""
        # C. Reviewer with inputs > 0: summary vacío -> semantic invalid
        rev_c = ReviewerReport(summary="")
        self.assertFalse(rev_c.is_valid_report())

        # F. Reviewer válido con required_testing=[] y required_docs=[] pero juicio suficiente -> DEBE poder ser valid
        rev_f = ReviewerReport(summary="Análisis exhaustivo sin necesidad de cambios.", v1_readiness="ready")
        self.assertTrue(rev_f.is_valid_report())

        # G. Accounting 10/10 por sí solo NO implica Reviewer valid
        # Even if accounting is complete, if summary is empty it is invalid
        rev_g = ReviewerReport(summary="   ", dispositions=[FindingDisposition(source_finding_id="b-001", disposition="rejected", reason="X")])
        self.assertFalse(rev_g.is_valid_report())

    def test_case_15_empty_reasons_in_dispositions_converted(self):
        """CASO D y E - Empty reasons in rejected/needs_verification dispositions are automatically populated with mandatory string."""
        spec = AgentReport(agent="architect", findings=[Finding(priority="P0", title="Arch 1", evidence="e.py:10", impact="Risk", recommendation="Fix")])
        spec.ensure_finding_ids()

        # D. needs_verification reason="" -> semantic invalid/repaired in accounting
        rev_d = ReviewerReport(
            summary="Reviewer plan",
            dispositions=[
                FindingDisposition(source_finding_id="architect-001", disposition="needs_verification", reason="")
            ],
        )
        reconciled_d, acc_d = reconcile_and_guarantee_accounting(rev_d, [spec])
        self.assertEqual(reconciled_d.dispositions[0].disposition, "needs_verification")
        self.assertIn("Reviewer omitted mandatory reason", reconciled_d.dispositions[0].reason)
        
        # E. needs_verification con razones específicas + explicación sustantiva -> valid
        rev_e = ReviewerReport(
            summary="Substantive reasoning here.",
            dispositions=[
                FindingDisposition(source_finding_id="architect-001", disposition="needs_verification", reason="Falta prueba en el archivo A.")
            ],
        )
        reconciled_e, acc_e = reconcile_and_guarantee_accounting(rev_e, [spec])
        self.assertEqual(reconciled_e.dispositions[0].reason, "Falta prueba en el archivo A.")

    def test_case_16_markdown_fallback_does_not_invent_evidence_or_placeholders(self):
        """9. Markdown fallback no inventa evidence/impact/recommendation."""
        raw_md = "### [P1] Missing database index\nSome text without explicit evidence or impact headings."
        from agent_team.models import _extract_findings_from_markdown_text
        findings = _extract_findings_from_markdown_text(raw_md)
        self.assertEqual(len(findings), 0)

        # Markdown with full valid sections extracts properly
        full_md = (
            "### [P0] SQL Injection in search query\n"
            "- Evidencia: src/search.py: raw query concatenation\n"
            "- Impacto: Exfiltración de datos sensibles en DB\n"
            "- Recomendación: Usar sentencias preparadas con parámetros\n"
        )
        valid_findings = _extract_findings_from_markdown_text(full_md)
        self.assertEqual(len(valid_findings), 1)
        self.assertEqual(valid_findings[0].title, "SQL Injection in search query")
        self.assertEqual(valid_findings[0].evidence, "src/search.py: raw query concatenation")

    def test_case_17_audit_with_failed_specialists_cannot_be_ready(self):
        """12. Audit con especialistas failed no puede considerarse READY."""
        spec_arch = AgentReport(agent="architect", summary="Arch OK", status="valid")
        spec_fail = AgentReport(agent="backend", summary="Backend Failed", status="failed")

        valid_specialists = [spec_arch, spec_fail]
        has_failed = any(s.status == "failed" for s in valid_specialists)

        rev = ReviewerReport(summary="Todo parece listo para producción.", v1_readiness="ready")
        if has_failed and rev.v1_readiness == "ready":
            rev.v1_readiness = "needs_verification"

        self.assertEqual(rev.v1_readiness, "needs_verification")

    def test_case_18_reviewer_raw_output_preserved(self):
        """11. Reviewer raw original se conserva."""
        raw_json = json.dumps({
            "agent": "reviewer",
            "summary": "Reviewer plan completado.",
            "v1_readiness": "ready",
            "final_findings": [],
            "dispositions": [],
        })
        rev, st = parse_reviewer_report(raw_json, [])
        self.assertIsNotNone(rev.raw_output)
        self.assertEqual(rev.raw_output, raw_json)

    def test_case_19_structured_all_invalid_findings_preserves_raw(self):
        """6 & 7. Structured parsed con findings todos inválidos no se pierde silenciosamente y conserva raw."""
        raw_json = json.dumps({
            "agent": "backend",
            "summary": "Analisis backend",
            "findings": [
                {"priority": "P1", "title": "N/A", "evidence": "N/A", "impact": "N/A", "recommendation": "N/A"}
            ],
        })
        rep, st = parse_agent_report("backend", raw_json)
        self.assertEqual(rep.raw_output, raw_json)
        self.assertEqual(len(rep.findings), 0)
        self.assertEqual(st, "failed")

    def test_case_20_format_recovery_does_not_invent_evidence(self):
        """8. Format recovery no inventa evidencia ni re-ejecuta auditoría."""
        raw_text = "Texto no estructurado sin JSON válido."
        rep, st = parse_agent_report("frontend", raw_text)
        self.assertEqual(len(rep.findings), 0)
        for f in rep.findings:
            self.assertTrue(f.is_valid_finding())

    def test_case_21_reviewer_to_final_markdown_renders_specialist_statuses(self):
        """Reviewer markdown report includes specialist execution statuses."""
        rev = ReviewerReport(summary="Executive summary here", v1_readiness="needs_verification")
        statuses = {"architect": "valid", "backend": "failed", "frontend": "valid"}
        md = rev.to_final_markdown("test-repo", "Audit goal", "qwen2.5", specialist_statuses=statuses)
        self.assertIn("### Specialist Execution", md)
        self.assertIn("- **Architect:** ✓ `valid`", md)
        self.assertIn("- **Backend:** ✗ `failed`", md)

    def test_zero_findings_rule(self):
        """1 & 2. Zero findings sin no_findings_reason es invalid; con razón sustantiva es valid."""
        # Sin no_findings_reason
        rep_invalid = AgentReport(agent="backend", summary="Revisión general realizada.", findings=[], no_findings_reason=None)
        self.assertFalse(rep_invalid.is_semantically_valid())

        rep_invalid_placeholder = AgentReport(agent="backend", summary="Revisión general realizada.", findings=[], no_findings_reason="N/A")
        self.assertFalse(rep_invalid_placeholder.is_semantically_valid())

        # Con no_findings_reason sustantivo
        rep_valid = AgentReport(
            agent="backend",
            summary="Auditoría completa de endpoints de autenticación y base de datos.",
            findings=[],
            no_findings_reason="Se auditaron todos los controladores y modelos; no se encontraron fallos P0/P1/P2.",
        )
        self.assertTrue(rep_valid.is_semantically_valid())

    def test_placeholder_elimination_across_fields(self):
        """3 & 4. Eliminación de placeholders en summary (p.ej. 'Resumen ejecutivo...') y open_questions."""
        self.assertFalse(is_meaningful_text("Resumen ejecutivo del estado del testing...", min_len=10))
        self.assertFalse(is_meaningful_text("Pregunta abierta sobre la suite...", min_len=10))
        self.assertFalse(is_meaningful_text("...", min_len=3))
        self.assertFalse(is_meaningful_text("Sin hallazgos", min_len=3))

        # Filter in AgentReport open_questions
        rep = AgentReport(
            agent="testing",
            summary="Reporte válido con análisis técnico adecuado de la suite.",
            no_findings_reason="No existen hallazgos de severidad crítica o alta.",
            open_questions=["Pregunta abierta sobre la suite...", "Existe un plan para migrar a Vitest?"],
        )
        self.assertEqual(len(rep.open_questions), 1)
        self.assertEqual(rep.open_questions[0], "Existe un plan para migrar a Vitest?")

    def test_evidence_first_cited_files_must_be_read(self):
        """5 & 6. Finding que cita archivo no leído es inválido; excepción para inventario/ausencia."""
        files_read = {"src/app.ts", "src/auth.ts"}
        known_tree = {"src/app.ts", "src/auth.ts", ".env.example"}

        # Cites unread file as code evidence
        f_bad = Finding(
            priority="P1",
            title="Insecure cookie config",
            evidence="In file .env.example line 5, cookie is set without secure flag",
            files=[".env.example"],
            impact="Cookie theft",
            recommendation="Add secure flag",
        )
        self.assertFalse(f_bad.is_valid_finding(files_included=files_read, known_inventory=known_tree))

        # Cites read file
        f_good = Finding(
            priority="P1",
            title="Insecure cookie config",
            evidence="In file src/auth.ts line 5, cookie is set without secure flag",
            files=["src/auth.ts"],
            impact="Cookie theft",
            recommendation="Add secure flag",
        )
        self.assertTrue(f_good.is_valid_finding(files_included=files_read, known_inventory=known_tree))

        # Explicit absence/inventory claim for unread file in tree
        f_absence = Finding(
            priority="P2",
            title="Falta archivo de configuración .env.example",
            evidence="El árbol de inventario revela ausencia de archivo de configuración de ejemplo",
            files=[".env.example"],
            impact="Dificulta configuración inicial",
            recommendation="Crear .env.example",
        )
        self.assertTrue(f_absence.is_valid_finding(files_included=files_read, known_inventory=known_tree))

    def test_llm_schemas_do_not_contain_pipeline_fields(self):
        """8 & 9. SpecialistLLMOutput y ReviewerLLMOutput no piden al LLM campos del pipeline."""
        from agent_team.models import (
            ReviewerLLMOutput,
            ReviewerUnresolvedSourceLLM,
            SpecialistFindingLLM,
            SpecialistLLMOutput,
        )

        spec_fields = set(SpecialistLLMOutput.model_fields.keys())
        self.assertNotIn("agent", spec_fields)
        self.assertNotIn("status", spec_fields)
        self.assertNotIn("retries", spec_fields)
        self.assertNotIn("raw_output", spec_fields)

        spec_finding_fields = set(SpecialistFindingLLM.model_fields.keys())
        self.assertNotIn("id", spec_finding_fields)

        rev_fields = set(ReviewerLLMOutput.model_fields.keys())
        self.assertNotIn("agent", rev_fields)
        self.assertNotIn("status", rev_fields)
        self.assertNotIn("retries", rev_fields)
        self.assertNotIn("raw_output", rev_fields)
        self.assertNotIn("dispositions", rev_fields)
        self.assertIn("unresolved_sources", rev_fields)

    def test_reviewer_retries_and_attempt_telemetry(self):
        """10, 11, 16. Telemetry and retry counter tracking per attempt."""
        from agent_team.graph import _extract_telemetry

        # Test telemetry extraction with and without metadata
        mock_raw_msg = type("MockMsg", (), {"response_metadata": {
            "prompt_eval_count": 1500,
            "eval_count": 350,
            "prompt_eval_duration": 1200000000,
            "eval_duration": 3500000000,
            "total_duration": 4700000000,
        }})()

        telem = _extract_telemetry(mock_raw_msg, 4.7)
        self.assertEqual(telem["prompt_eval_count"], 1500)
        self.assertEqual(telem["eval_count"], 350)
        self.assertEqual(telem["duration_seconds"], 4.7)

        # Missing metadata should not break execution
        telem_empty = _extract_telemetry(None, 2.1)
        self.assertIsNone(telem_empty["prompt_eval_count"])
        self.assertEqual(telem_empty["duration_seconds"], 2.1)

    def test_num_ctx_configuration_and_manifest(self):
        """OLLAMA_NUM_CTX default es 16384, se puede sobreescribir por env y se persiste en manifest."""
        import os
        from agent_team.config import load_settings
        from agent_team.repo_context import ROLE_CHAR_LIMITS

        # Default is 16384
        with patch.dict(os.environ, {}, clear=False):
            if "OLLAMA_NUM_CTX" in os.environ:
                del os.environ["OLLAMA_NUM_CTX"]
            settings_default = load_settings()
            self.assertEqual(settings_default.num_ctx, 16384)

        # Overridden via env
        with patch.dict(os.environ, {"OLLAMA_NUM_CTX": "8192"}):
            settings_custom = load_settings()
            self.assertEqual(settings_custom.num_ctx, 8192)

        # Role context budgets
        self.assertEqual(ROLE_CHAR_LIMITS["architect"], 22000)
        self.assertEqual(ROLE_CHAR_LIMITS["backend"], 24000)
        self.assertEqual(ROLE_CHAR_LIMITS["frontend"], 24000)
        self.assertEqual(ROLE_CHAR_LIMITS["testing"], 22000)
        self.assertEqual(ROLE_CHAR_LIMITS["docs"], 18000)
        self.assertEqual(ROLE_CHAR_LIMITS["reviewer"], 20000)

        with tempfile.TemporaryDirectory() as tmpdir:
            base_out = Path(tmpdir)
            run_ctx = init_run(base_out, timestamp="20260827-230000")
            rev_rep = ReviewerReport(summary="All clean")
            save_manifest(
                run_ctx=run_ctx,
                repo_name="demo",
                repo_path="/fake/path",
                goal="Audit",
                model="test-model",
                num_ctx=16384,
                role_metrics={},
                reviewer_report=rev_rep,
            )
            manifest_data = json.loads(run_ctx.manifest_file.read_text(encoding="utf-8"))
            self.assertEqual(manifest_data["num_ctx"], 16384)


    def test_closure_reviewer_llm_output_deterministic_resolution(self):
        """CLOSURE 1 — ReviewerLLMOutput con source_finding_ids produce final finding reviewer-001 y final_finding_id=reviewer-001."""
        from agent_team.models import (
            ReviewerFinalFindingLLM,
            ReviewerLLMOutput,
            ReviewerUnresolvedSourceLLM,
            derive_dispositions_from_reviewer_output,
        )

        b_rep = AgentReport(agent="backend", findings=[Finding(priority="P0", title="Auth bug", evidence="auth.py:10", impact="Risk", recommendation="Fix")])
        b_rep.ensure_finding_ids()

        dto = ReviewerLLMOutput(
            summary="Reviewer plan sustantivo para la versión 1.0.",
            v1_readiness="not_ready",
            v1_readiness_reason="Falta resolver auth blocker.",
            final_findings=[
                ReviewerFinalFindingLLM(
                    source_finding_ids=["backend-001"],
                    priority="P0",
                    title="Auth bug",
                    evidence="auth.py:10",
                    files=["auth.py"],
                    impact="Risk",
                    recommendation="Fix",
                )
            ],
            unresolved_sources=[],
        )

        conv_findings = [Finding(**f.model_dump()) for f in dto.final_findings]
        for idx, f in enumerate(conv_findings, 1):
            f.id = f"reviewer-{idx:03d}"
        derived_disps, _ = derive_dispositions_from_reviewer_output(conv_findings, dto.unresolved_sources, {"backend-001"})
        report = ReviewerReport(
            agent="reviewer",
            summary=dto.summary,
            v1_readiness=dto.v1_readiness,
            v1_readiness_reason=dto.v1_readiness_reason,
            final_findings=conv_findings,
            dispositions=derived_disps,
        )

        reconciled, acc = reconcile_and_guarantee_accounting(report, [b_rep])

        self.assertEqual(len(reconciled.final_findings), 1)
        self.assertEqual(reconciled.final_findings[0].id, "reviewer-001")
        self.assertEqual(reconciled.dispositions[0].disposition, "accepted")
        self.assertEqual(reconciled.dispositions[0].final_finding_id, "reviewer-001")
        self.assertEqual(acc.accounted_count, 1)
        self.assertEqual(acc.accepted_count, 1)
        self.assertTrue(acc.is_fully_accounted)

    def test_closure_format_recovery_zero_findings_without_reason_fails(self):
        """CLOSURE 3 — Format recovery con findings=[] sin no_findings_reason resulta en failed."""
        raw_json = json.dumps({
            "summary": "Resumen ejecutivo del backend.",
            "no_findings_reason": None,
            "findings": [],
        })
        rep, status = parse_agent_report("backend", raw_json, retries=1)
        self.assertEqual(status, "failed")
        self.assertEqual(rep.status, "failed")

    def test_closure_format_recovery_unread_file_evidence_fails(self):
        """CLOSURE 4 — Format recovery con finding que cita archivo no leído resulta en failed."""
        raw_json = json.dumps({
            "summary": "Resumen ejecutivo del backend.",
            "findings": [
                {
                    "priority": "P1",
                    "title": "Config error",
                    "evidence": "In unread file database.config.ts line 20",
                    "files": ["database.config.ts"],
                    "impact": "Config error",
                    "recommendation": "Fix",
                }
            ],
        })
        files_read = {"src/routes.py"}
        rep, status = parse_agent_report("backend", raw_json, retries=1, files_included=files_read)
        self.assertEqual(status, "failed")
        self.assertEqual(rep.status, "failed")

    def test_closure_reviewer_retry_semantically_invalid_fails(self):
        """CLOSURE 5 — Reviewer retry todavía semánticamente inválido resulta en status=failed, NO repaired."""
        # Report with empty summary after retry
        rep = ReviewerReport(summary="   ", v1_readiness="needs_verification")
        self.assertFalse(rep.is_valid_report())
        if not rep.is_valid_report():
            rep.status = "failed"
        self.assertEqual(rep.status, "failed")

    def test_closure_reviewer_final_finding_outside_targeted_context_invalid(self):
        """CLOSURE 6 — Reviewer final finding que cita archivo fuera de targeted context no es válido."""
        targeted_files = {"src/server.ts"}
        known_tree = {"src/server.ts", "package.json"}

        f_outside = Finding(
            priority="P1",
            title="Secret leaked in file",
            evidence="In file unread_secret.key line 1 private key is plain text",
            files=["unread_secret.key"],
            impact="Data breach",
            recommendation="Remove key",
        )
        self.assertFalse(f_outside.is_valid_finding(files_included=targeted_files, known_inventory=known_tree))


if __name__ == "__main__":
    unittest.main()
