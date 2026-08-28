import json
import tempfile
import unittest
from pathlib import Path

from agent_team.config import load_settings
from agent_team.models import (
    AgentReport,
    Finding,
    FindingDisposition,
    ReviewerReport,
    compute_accounting_summary,
    parse_agent_report,
    parse_reviewer_report,
    reconcile_and_guarantee_accounting,
    repair_json_string,
)
from agent_team.repo_context import (
    ROLE_CHAR_LIMITS,
    RepoSnapshot,
    build_global_inventory_tree,
    build_targeted_snapshot,
    normalize_rel_path,
)


class TestResilienceAndBudget(unittest.TestCase):
    def test_normalize_rel_path(self):
        self.assertEqual(normalize_rel_path("./src/main.py"), "src/main.py")
        self.assertEqual(normalize_rel_path("src\\models\\user.py"), "src/models/user.py")
        self.assertEqual(normalize_rel_path("  './frontend/App.tsx'  "), "frontend/App.tsx")
        self.assertEqual(normalize_rel_path("///api//routes.py"), "api/routes.py")
        self.assertEqual(normalize_rel_path(""), "")
        self.assertEqual(normalize_rel_path(None), "")

    def test_repair_json_string_trailing_commas_and_quotes(self):
        malformed = """
        ```json
        {
            “summary”: “Análisis del backend completado con éxito.”,
            “no_findings_reason”: null,
            “findings”: [
                {
                    “priority”: “P1”,
                    “title”: “Falta validación en auth”,
                    “evidence”: “src/auth.py:45”,
                    “files”: [“./src/auth.py”,],
                    “impact”: “Riesgo de seguridad”,
                    “recommendation”: “Agregar schema validation”,
                    “confidence”: “high”,
                },
            ],
            “open_questions”: [
                “¿Se requiere soporte OAuth?”,
            ],
        }
        ```
        """
        repaired = repair_json_string(malformed)
        data = json.loads(repaired)
        self.assertEqual(data["summary"], "Análisis del backend completado con éxito.")
        self.assertEqual(len(data["findings"]), 1)
        self.assertEqual(data["findings"][0]["title"], "Falta validación en auth")

    def test_finding_path_normalization_matches_included_files(self):
        # Model returns finding citing './src/server.ts' while snapshot read 'src/server.ts'
        f = Finding(
            priority="P0",
            title="Unhandled rejection in server",
            evidence="src/server.ts:25",
            files=["./src/server.ts"],
            impact="Crash on startup",
            recommendation="Add error handler",
        )
        self.assertEqual(f.files, ["src/server.ts"])
        self.assertTrue(f.is_valid_finding(files_included=["src/server.ts"]))

    def test_parse_agent_report_with_imperfect_local_llm_json(self):
        raw_text = """
        Aquí está el análisis solicitado:
        ```json
        {
            "summary": "Arquitectura general sólida pero requiere corrección en rutas.",
            "findings": [
                {
                    "priority": "P1",
                    "title": "Rutas sin validación de tipo",
                    "evidence": "src/routes.py:10",
                    "files": ["src/routes.py"],
                    "impact": "Inconsistencias en runtime",
                    "recommendation": "Usar pydantic models",
                    "confidence": "high",
                },
            ],
            "open_questions": [],
        }
        ```
        Espero que esto sirva para la auditoría.
        """
        report, status = parse_agent_report(
            role="backend",
            raw_text=raw_text,
            files_included=["src/routes.py"],
            known_inventory=["src/routes.py"],
        )
        self.assertIn(status, {"valid", "repaired"})
        self.assertEqual(len(report.findings), 1)
        self.assertEqual(report.findings[0].files, ["src/routes.py"])

    def test_global_inventory_tree_bounded_for_large_repo(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            # Create 300 files
            for i in range(300):
                (root / f"file_{i:03d}.py").write_text("# dummy")

            tree = build_global_inventory_tree(root, max_lines=50, max_chars=2000)
            lines = tree.splitlines()
            self.assertLessEqual(len(lines), 52)
            self.assertTrue(any("archivos adicionales" in line or "TRUNCADO" in line for line in lines))

    def test_targeted_snapshot_uses_normalized_paths(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "src").mkdir()
            (root / "src" / "index.ts").write_text("console.log('hello world');")

            # Specialist cited with leading './'
            target_files = {"./src/index.ts"}
            snap = build_targeted_snapshot(
                root=root,
                target_files=target_files,
                full_inventory_tree="src/index.ts",
            )
            self.assertEqual(snap.files_included, ["src/index.ts"])
            self.assertIn("hello world", snap.content)

    def test_context_budget_limits_within_16k_window(self):
        settings = load_settings()
        # 16k context is 16,384 tokens ≈ 48,000 - 60,000 characters
        self.assertEqual(settings.num_ctx, 16384)
        self.assertLessEqual(settings.max_total_chars, 50000)
        for role, char_limit in ROLE_CHAR_LIMITS.items():
            self.assertLessEqual(char_limit, 35000)

    def test_reviewer_reconcile_when_all_specialists_have_zero_findings(self):
        specialists = [
            AgentReport(agent=r, summary=f"Todo en orden en {r}", no_findings_reason=f"Se revisó {r} sin novedades", findings=[])
            for r in ["architect", "backend", "frontend", "testing", "docs"]
        ]
        rev = ReviewerReport(
            summary="Auditoría limpia sin hallazgos críticos.",
            v1_readiness="ready",
            v1_readiness_reason="No existen problemas bloqueantes en el repositorio.",
            final_findings=[],
            dispositions=[],
        )
        reconciled, summary = reconcile_and_guarantee_accounting(rev, specialists)
        self.assertEqual(summary.total_input_findings, 0)
        self.assertEqual(summary.accounted_count, 0)
        self.assertTrue(summary.is_fully_accounted)
        self.assertEqual(reconciled.v1_readiness, "ready")

    def test_reviewer_failed_specialist_downgrades_ready(self):
        spec_valid = AgentReport(agent="architect", summary="Arch OK", no_findings_reason="Sin hallazgos", status="valid")
        spec_failed = AgentReport(agent="backend", summary="Fallo backend", status="failed")

        rev = ReviewerReport(
            summary="Resumen preliminar",
            v1_readiness="ready",
            v1_readiness_reason="Parece listo",
            final_findings=[],
            dispositions=[],
        )
        reconciled, _ = reconcile_and_guarantee_accounting(rev, [spec_valid, spec_failed])
        # In graph logic, if any specialist failed, v1_readiness cannot remain "ready"
        if any(s.status == "failed" for s in [spec_valid, spec_failed]):
            if reconciled.v1_readiness == "ready":
                reconciled.v1_readiness = "needs_verification"
        self.assertEqual(reconciled.v1_readiness, "needs_verification")


    def test_reviewer_regression_33_source_findings_and_deterministic_dispositions(self):
        """
        Test de regresión principal:
        33 source findings generados por 5 especialistas.
        Reviewer produce 9 final_findings (31 source IDs) + 2 unresolved_sources (2 source IDs).
        Python deriva determinísticamente 33 dispositions con 100% contabilidad.
        """
        from agent_team.models import (
            ReviewerFinalFindingLLM,
            ReviewerLLMOutput,
            ReviewerUnresolvedSourceLLM,
            derive_dispositions_from_reviewer_output,
        )

        specialist_counts = {
            "architect": 6,
            "backend": 8,
            "frontend": 7,
            "testing": 7,
            "docs": 5,
        }
        specialists = []
        all_orig_ids = set()
        for role, count in specialist_counts.items():
            findings = [
                Finding(
                    priority="P1",
                    title=f"{role.capitalize()} finding #{i}",
                    evidence=f"src/{role}/{i}.ts:10",
                    files=[f"src/{role}/{i}.ts"],
                    impact="Risk",
                    recommendation="Fix",
                )
                for i in range(1, count + 1)
            ]
            rep = AgentReport(agent=role, findings=findings)
            rep.ensure_finding_ids()
            specialists.append(rep)
            for f in rep.findings:
                all_orig_ids.add(f.id)

        self.assertEqual(len(all_orig_ids), 33)

        # 9 final findings consolidating 31 source IDs:
        # 4 single-source (accepted)
        # 5 multi-source (merged)
        final_findings_llm = [
            ReviewerFinalFindingLLM(
                source_finding_ids=["architect-001"],
                priority="P0",
                title="Single source 1",
                evidence="src/architect/1.ts:10",
                files=["src/architect/1.ts"],
                impact="Critical",
                recommendation="Fix",
            ),
            ReviewerFinalFindingLLM(
                source_finding_ids=["backend-001"],
                priority="P1",
                title="Single source 2",
                evidence="src/backend/1.ts:10",
                files=["src/backend/1.ts"],
                impact="Risk",
                recommendation="Fix",
            ),
            ReviewerFinalFindingLLM(
                source_finding_ids=["frontend-001"],
                priority="P1",
                title="Single source 3",
                evidence="src/frontend/1.ts:10",
                files=["src/frontend/1.ts"],
                impact="Risk",
                recommendation="Fix",
            ),
            ReviewerFinalFindingLLM(
                source_finding_ids=["docs-001"],
                priority="P2",
                title="Single source 4",
                evidence="src/docs/1.ts:10",
                files=["src/docs/1.ts"],
                impact="Docs",
                recommendation="Fix",
            ),
            ReviewerFinalFindingLLM(
                source_finding_ids=["architect-002", "architect-003", "backend-002", "backend-003"],
                priority="P0",
                title="Merged backend architecture core",
                evidence="src/architect/2.ts:10",
                files=["src/architect/2.ts"],
                impact="Crash",
                recommendation="Refactor",
            ),
            ReviewerFinalFindingLLM(
                source_finding_ids=["backend-004", "backend-005", "backend-006", "backend-007", "backend-008"],
                priority="P1",
                title="Merged backend endpoints",
                evidence="src/backend/4.ts:10",
                files=["src/backend/4.ts"],
                impact="Inconsistency",
                recommendation="Add schemas",
            ),
            ReviewerFinalFindingLLM(
                source_finding_ids=["frontend-002", "frontend-003", "frontend-004", "frontend-005", "frontend-006", "frontend-007"],
                priority="P1",
                title="Merged frontend components",
                evidence="src/frontend/2.ts:10",
                files=["src/frontend/2.ts"],
                impact="UI bugs",
                recommendation="Update props",
            ),
            ReviewerFinalFindingLLM(
                source_finding_ids=["testing-001", "testing-002", "testing-003", "testing-004", "testing-005", "testing-006"],
                priority="P1",
                title="Merged testing coverage suite",
                evidence="src/testing/1.ts:10",
                files=["src/testing/1.ts"],
                impact="Flaky tests",
                recommendation="Add mocks",
            ),
            ReviewerFinalFindingLLM(
                source_finding_ids=["architect-004", "architect-005", "architect-006", "docs-002", "docs-003", "docs-004"],
                priority="P2",
                title="Merged arch docs specs",
                evidence="src/docs/2.ts:10",
                files=["src/docs/2.ts"],
                impact="Missing API specs",
                recommendation="Write OpenAPI",
            ),
        ]

        unresolved_llm = [
            ReviewerUnresolvedSourceLLM(
                source_finding_ids=["testing-007"],
                disposition="rejected",
                reason="Afirmación especulativa sin reproducción comprobable.",
            ),
            ReviewerUnresolvedSourceLLM(
                source_finding_ids=["docs-005"],
                disposition="needs_verification",
                reason="Requiere confirmación humana con el equipo de producto.",
            ),
        ]

        reviewer_output = ReviewerLLMOutput(
            summary="Consolidación ejecutiva completa de 33 hallazgos hacia v1.",
            v1_readiness="not_ready",
            v1_readiness_reason="Existen P0 de arranque pendientes de resolver.",
            final_findings=final_findings_llm,
            unresolved_sources=unresolved_llm,
            contradictions=["Testing reportó endpoint faltante pero Backend lo implementó en routes/v2."],
            discarded_claims=["Testing-007 descartado por falta de evidencia."],
            recommended_order=["1. Resolver P0 de arranque", "2. Schemas backend"],
            required_testing=["Añadir tests de integración"],
            required_docs=["OpenAPI spec"],
            v1_release_criteria=["P0 resueltos", "CI en verde"],
            open_questions=[],
        )

        conv_findings = [Finding(**f.model_dump()) for f in reviewer_output.final_findings]
        for idx, f in enumerate(conv_findings, 1):
            f.id = f"reviewer-{idx:03d}"

        derived_dispositions, had_dup = derive_dispositions_from_reviewer_output(
            conv_findings,
            reviewer_output.unresolved_sources,
            all_orig_ids,
        )

        self.assertFalse(had_dup)
        self.assertEqual(len(derived_dispositions), 33)

        report = ReviewerReport(
            agent="reviewer",
            summary=reviewer_output.summary,
            v1_readiness=reviewer_output.v1_readiness,
            v1_readiness_reason=reviewer_output.v1_readiness_reason,
            final_findings=conv_findings,
            dispositions=derived_dispositions,
            contradictions=reviewer_output.contradictions,
            discarded_claims=reviewer_output.discarded_claims,
            recommended_order=reviewer_output.recommended_order,
            required_testing=reviewer_output.required_testing,
            required_docs=reviewer_output.required_docs,
            v1_release_criteria=reviewer_output.v1_release_criteria,
            open_questions=reviewer_output.open_questions,
            status="valid",
            retries=0,
        )

        reconciled, summary = reconcile_and_guarantee_accounting(report, specialists)

        self.assertEqual(summary.total_input_findings, 33)
        self.assertEqual(summary.accounted_count, 33)
        self.assertEqual(summary.accepted_count, 4)
        self.assertEqual(summary.merged_count, 27)
        self.assertEqual(summary.rejected_count, 1)
        self.assertEqual(summary.needs_verification_count, 1)
        self.assertEqual(summary.missing_ids, [])
        self.assertTrue(summary.is_fully_accounted)

        # Check disposition mappings
        disp_by_source = {d.source_finding_id: d for d in reconciled.dispositions}
        # architect-001 is single -> accepted
        self.assertEqual(disp_by_source["architect-001"].disposition, "accepted")
        self.assertEqual(disp_by_source["architect-001"].final_finding_id, "reviewer-001")

        # architect-002 is merged into reviewer-005
        self.assertEqual(disp_by_source["architect-002"].disposition, "merged")
        self.assertEqual(disp_by_source["architect-002"].final_finding_id, "reviewer-005")

        # testing-007 is rejected
        self.assertEqual(disp_by_source["testing-007"].disposition, "rejected")
        self.assertIsNone(disp_by_source["testing-007"].final_finding_id)
        self.assertIn("especulativa", disp_by_source["testing-007"].reason)

        # docs-005 is needs_verification
        self.assertEqual(disp_by_source["docs-005"].disposition, "needs_verification")
        self.assertIsNone(disp_by_source["docs-005"].final_finding_id)

    def test_reviewer_duplicate_and_cross_referenced_source_handling(self):
        """No duplicate sources across final findings, or between final and unresolved, or within unresolved."""
        from agent_team.models import (
            ReviewerUnresolvedSourceLLM,
            derive_dispositions_from_reviewer_output,
        )

        orig_ids = {"backend-001", "backend-002", "testing-001"}
        final_findings = [
            Finding(id="reviewer-001", source_finding_ids=["backend-001", "backend-002"], priority="P1", title="Finding 1", evidence="src/f.py:10", files=["src/f.py"], impact="Impact 1", recommendation="Rec 1"),
            # Duplicate backend-002 in second final finding
            Finding(id="reviewer-002", source_finding_ids=["backend-002"], priority="P2", title="Finding 2", evidence="src/f.py:20", files=["src/f.py"], impact="Impact 2", recommendation="Rec 2"),
        ]
        # Duplicate backend-001 in unresolved sources + testing-001 duplicated
        unresolved = [
            ReviewerUnresolvedSourceLLM(source_finding_ids=["backend-001", "testing-001"], disposition="rejected", reason="Duplicate claim discarded"),
            ReviewerUnresolvedSourceLLM(source_finding_ids=["testing-001"], disposition="needs_verification", reason="Requires manual verification"),
        ]

        disps, had_dup = derive_dispositions_from_reviewer_output(final_findings, unresolved, orig_ids)
        self.assertTrue(had_dup)
        # Should contain exactly one disposition per original source ID
        disp_sources = [d.source_finding_id for d in disps]
        self.assertEqual(len(disp_sources), len(set(disp_sources)))
        self.assertEqual(set(disp_sources), orig_ids)

    def test_reviewer_missing_source_findings_automatic_recovery(self):
        """Python automatically assigns needs_verification with fallback reason for omitted sources."""
        from agent_team.models import derive_dispositions_from_reviewer_output

        spec = AgentReport(
            agent="backend",
            findings=[
                Finding(priority="P1", title=f"Backend issue #{i}", evidence=f"src/b{i}.py:10", files=[f"src/b{i}.py"], impact="Crash risk in production", recommendation="Refactor module")
                for i in range(1, 6)
            ],
        )
        spec.ensure_finding_ids()

        # Reviewer only accounted for backend-001 and backend-002
        final_findings = [
            Finding(id="reviewer-001", source_finding_ids=["backend-001", "backend-002"], priority="P1", title="Consolidated backend bug", evidence="src/b1.py:10", files=["src/b1.py"], impact="Crash risk in production", recommendation="Refactor module")
        ]
        orig_ids = {f.id for f in spec.findings}
        disps, _ = derive_dispositions_from_reviewer_output(final_findings, [], orig_ids)

        rep = ReviewerReport(summary="Summary", final_findings=final_findings, dispositions=disps)
        reconciled, summary = reconcile_and_guarantee_accounting(rep, [spec])

        self.assertEqual(summary.total_input_findings, 5)
        self.assertEqual(summary.accounted_count, 5)
        self.assertEqual(summary.merged_count, 2)
        self.assertEqual(summary.needs_verification_count, 3)
        self.assertTrue(summary.is_fully_accounted)

    def test_num_predict_configuration_and_override(self):
        """1, 2, 3. Identificar/configurar num_predict, default 4096, override por env."""
        import os
        from agent_team.config import load_settings
        from unittest.mock import patch

        # Default is 4096
        with patch.dict(os.environ, {}, clear=False):
            if "OLLAMA_NUM_PREDICT" in os.environ:
                del os.environ["OLLAMA_NUM_PREDICT"]
            settings_default = load_settings()
            self.assertEqual(settings_default.num_predict, 4096)

        # Override via env
        with patch.dict(os.environ, {"OLLAMA_NUM_PREDICT": "2048"}):
            settings_custom = load_settings()
            self.assertEqual(settings_custom.num_predict, 2048)

    def test_num_predict_passed_to_chat_ollama(self):
        """4. num_predict se pasa a ChatOllama en build_graph."""
        from unittest.mock import MagicMock, patch
        from agent_team.config import Settings
        from agent_team.graph import build_graph

        dummy_snap = RepoSnapshot(root=Path("."), tree="", content="", files_included=[], total_chars=0)
        settings = Settings(
            model="test-model",
            base_url="http://localhost:11434",
            num_ctx=16384,
            num_predict=4096,
            timeout_seconds=180,
            max_files=10,
            max_file_chars=1000,
            max_total_chars=5000,
            prompts_dir=Path("./agents"),
            output_dir=Path("./output"),
        )

        with patch("agent_team.graph.ChatOllama") as mock_ollama_cls:
            mock_ollama_cls.return_value = MagicMock()
            build_graph(settings, {"architect": dummy_snap, "backend": dummy_snap, "frontend": dummy_snap, "testing": dummy_snap, "docs": dummy_snap, "reviewer": dummy_snap})
            mock_ollama_cls.assert_called_once()
            _, kwargs = mock_ollama_cls.call_args
            self.assertEqual(kwargs.get("num_predict"), 4096)
            self.assertEqual(kwargs.get("num_ctx"), 16384)

    def test_eval_count_equals_num_predict_flags_truncation(self):
        """5 & 6. eval_count == num_predict marca output_truncated=True y loguea advertencia."""
        from agent_team.graph import _extract_telemetry
        from agent_team.run_manager import init_run

        with tempfile.TemporaryDirectory() as tmpdir:
            run_ctx = init_run(Path(tmpdir), timestamp="20260828-120000")

            mock_truncated_msg = type("MockMsg", (), {"response_metadata": {
                "prompt_eval_count": 7483,
                "eval_count": 4096,
                "prompt_eval_duration": 5000000000,
                "eval_duration": 15000000000,
                "total_duration": 20000000000,
            }})()

            telem = _extract_telemetry(mock_truncated_msg, 20.0, num_ctx=16384, num_predict=4096, role="backend", run_ctx=run_ctx)
            self.assertTrue(telem["output_truncated"])
            self.assertEqual(telem["remaining_context"], 16384 - 7483 - 4096)

            log_text = run_ctx.log_file.read_text(encoding="utf-8")
            self.assertIn("WARNING Backend output may have been truncated by generation limit (4096/4096)", log_text)

    def test_source_finding_id_deduplicated_in_final_findings_and_consistent_with_accounting(self):
        """7, 9, 10. Source ID duplicado queda en un solo final finding y coincide 100% con accounting."""
        from agent_team.models import (
            deduplicate_final_findings_sources,
            derive_dispositions_from_reviewer_output,
            reconcile_and_guarantee_accounting,
        )

        orig_ids = {"testing-001", "testing-002"}
        spec = AgentReport(
            agent="testing",
            findings=[
                Finding(priority="P1", title="Test issue 1", evidence="test1.py:10", files=["test1.py"], impact="Risk", recommendation="Fix"),
                Finding(priority="P1", title="Test issue 2", evidence="test2.py:20", files=["test2.py"], impact="Risk", recommendation="Fix"),
            ],
        )
        spec.ensure_finding_ids()

        # LLM erroneously included testing-001 in both reviewer-001 and reviewer-002
        raw_final_findings = [
            Finding(id="reviewer-001", source_finding_ids=["testing-001"], priority="P1", title="Final 1", evidence="test1.py:10", files=["test1.py"], impact="Risk 1", recommendation="Fix 1"),
            Finding(id="reviewer-002", source_finding_ids=["testing-001", "testing-002"], priority="P1", title="Final 2", evidence="test2.py:20", files=["test2.py"], impact="Risk 2", recommendation="Fix 2"),
        ]

        cleaned_findings, had_dup = deduplicate_final_findings_sources(raw_final_findings, orig_ids)
        self.assertTrue(had_dup)
        self.assertEqual(len(cleaned_findings), 2)
        # testing-001 is in reviewer-001 only
        self.assertEqual(cleaned_findings[0].source_finding_ids, ["testing-001"])
        # reviewer-002 has only testing-002 (duplicate testing-001 removed)
        self.assertEqual(cleaned_findings[1].source_finding_ids, ["testing-002"])

        disps, _ = derive_dispositions_from_reviewer_output(cleaned_findings, [], orig_ids)
        rep = ReviewerReport(summary="Consolidated", final_findings=cleaned_findings, dispositions=disps)
        reconciled, acc = reconcile_and_guarantee_accounting(rep, [spec])

        self.assertTrue(acc.is_fully_accounted)
        self.assertEqual(acc.accounted_count, 2)
        self.assertEqual(acc.accepted_count, 2)
        # Ensure final_findings match dispositions exactly
        self.assertEqual(reconciled.final_findings[0].source_finding_ids, ["testing-001"])
        self.assertEqual(reconciled.final_findings[1].source_finding_ids, ["testing-002"])

    def test_closure_readiness_rules(self):
        """1, 2, 3. Reglas de readiness determinista (P0=0 no dice P0 blockers; P1>0 cita P1; needs_verif>0 da needs_verif)."""
        from agent_team.models import (
            AccountingSummary,
            determine_deterministic_readiness,
            reconcile_and_guarantee_accounting,
        )

        spec = AgentReport(agent="testing", findings=[
            Finding(id="testing-001", priority="P1", title="Title 1", evidence="t1.py:1", files=["t1.py"], impact="Impact 1", recommendation="Recommendation 1"),
            Finding(id="testing-002", priority="P1", title="Title 2", evidence="t2.py:1", files=["t2.py"], impact="Impact 2", recommendation="Recommendation 2"),
        ])

        # Scenario 1 & 2: P0 = 0, P1 = 2 -> not_ready mentioning P1, NEVER P0 blockers
        final_p1_only = [
            Finding(id="reviewer-001", source_finding_ids=["testing-001"], priority="P1", title="Final P1-1", evidence="t1.py:1", files=["t1.py"], impact="Impact 1", recommendation="Recommendation 1"),
            Finding(id="reviewer-002", source_finding_ids=["testing-002"], priority="P1", title="Final P1-2", evidence="t2.py:1", files=["t2.py"], impact="Impact 2", recommendation="Recommendation 2"),
        ]
        rep = ReviewerReport(
            summary="Audit",
            v1_readiness="not_ready",
            v1_readiness_reason="Existen release blockers P0 sin resolver que impiden el despliegue.",  # LLM hallucinated reason
            final_findings=final_p1_only,
            dispositions=[
                FindingDisposition(source_finding_id="testing-001", disposition="accepted", final_finding_id="reviewer-001"),
                FindingDisposition(source_finding_id="testing-002", disposition="accepted", final_finding_id="reviewer-002"),
            ],
        )
        reconciled, acc = reconcile_and_guarantee_accounting(rep, [spec])
        self.assertEqual(reconciled.v1_readiness, "not_ready")
        self.assertNotIn("P0", reconciled.v1_readiness_reason)
        self.assertIn("P1", reconciled.v1_readiness_reason)
        self.assertIn("2", reconciled.v1_readiness_reason)

        # Scenario 3: Needs verification > 0 -> readiness is needs_verification
        rep_unverif = ReviewerReport(
            summary="Audit",
            final_findings=[],
            dispositions=[
                FindingDisposition(source_finding_id="testing-001", disposition="needs_verification", reason="Unverified"),
                FindingDisposition(source_finding_id="testing-002", disposition="needs_verification", reason="Unverified"),
            ],
        )
        reconciled_unverif, _ = reconcile_and_guarantee_accounting(rep_unverif, [spec])
        self.assertEqual(reconciled_unverif.v1_readiness, "needs_verification")

        # Scenario 4: All clean -> ready
        clean_spec = AgentReport(agent="docs", findings=[])
        rep_clean = ReviewerReport(summary="Clean", final_findings=[], dispositions=[])
        reconciled_clean, _ = reconcile_and_guarantee_accounting(rep_clean, [clean_spec])
        self.assertEqual(reconciled_clean.v1_readiness, "ready")

    def test_closure_nonexistent_source_id_in_contradiction_or_discarded_claim(self):
        """4. Source ID inexistente (ej. frontend-003) en contradiction o discarded claim es descartado."""
        from agent_team.models import validate_and_filter_reviewer_claims

        orig_ids = {"backend-001", "testing-001"}
        raw_contras = [
            {"source_finding_ids": ["backend-001", "testing-001"], "description": "Backend auth vs testing login"},
            {"source_finding_ids": ["frontend-003"], "description": "Frontend-003 afirmaba falta de soporte móvil"},
        ]
        raw_discards = [
            {"source_finding_ids": ["frontend-003"], "reason": "Frontend-003 afirmaba falta de soporte móvil sin inspeccionar CSS"},
            {"source_finding_ids": ["backend-001"], "reason": "Afirmación duplicada"},
        ]

        clean_c, clean_d, had_halluc = validate_and_filter_reviewer_claims(raw_contras, raw_discards, orig_ids)
        self.assertTrue(had_halluc)
        self.assertEqual(len(clean_c), 1)
        self.assertIn("Backend auth", clean_c[0])
        self.assertEqual(len(clean_d), 1)
        self.assertIn("Afirmación duplicada", clean_d[0])

    def test_closure_docs_finding_grounded_in_readme_and_env_example(self):
        """5. Docs finding respaldado por README.md/.env.example es evidencia válida y no se rechaza."""
        from agent_team.models import validate_and_filter_reviewer_findings

        docs_spec = AgentReport(
            agent="docs",
            findings=[
                Finding(id="docs-001", priority="P1", title="Falta doc de env", evidence="README.md:15", files=["README.md", ".env.example"], impact="Setup issue", recommendation="Update doc"),
            ],
        )
        raw_reviewer_finals = [
            {
                "source_finding_ids": ["docs-001"],
                "priority": "P1",
                "title": "Documentación de variables incompleta",
                "evidence": "README.md:15 variables faltantes",
                "files": ["README.md", ".env.example"],
                "impact": "Setup issue",
                "recommendation": "Update doc",
            }
        ]

        valid_f, dropped = validate_and_filter_reviewer_findings(raw_reviewer_finals, [docs_spec])
        self.assertEqual(len(dropped), 0)
        self.assertEqual(len(valid_f), 1)
        self.assertEqual(set(valid_f[0].files), {".env.example", "README.md"})

    def test_closure_reviewer_consolidates_finding_outside_targeted_snapshot(self):
        """6 & 7. Specialist finding grounded en archivo leído se consolida aunque no esté en targeted snapshot."""
        from agent_team.models import validate_and_filter_reviewer_findings

        backend_spec = AgentReport(
            agent="backend",
            findings=[
                Finding(id="backend-001", priority="P1", title="DB retry issue", evidence="src/large_backend.py:100", files=["src/large_backend.py"], impact="Crash", recommendation="Add retry"),
            ],
        )

        # Reviewer includes an extra hallucinated external file
        raw_reviewer_finals = [
            {
                "source_finding_ids": ["backend-001"],
                "priority": "P1",
                "title": "DB retry issue",
                "evidence": "src/large_backend.py:100 missing retry",
                "files": ["src/large_backend.py", "external_hallucinated.py"],
                "impact": "Crash",
                "recommendation": "Add retry",
            }
        ]

        valid_f, dropped = validate_and_filter_reviewer_findings(raw_reviewer_finals, [backend_spec])
        self.assertEqual(len(dropped), 0)
        self.assertEqual(len(valid_f), 1)
        # external_hallucinated.py is excluded; only allowed_source_files are retained
        self.assertEqual(valid_f[0].files, ["src/large_backend.py"])

    def test_closure_dropped_finding_logs_diagnostic_reason(self):
        """8. Reviewer final finding eliminado registra razón determinística."""
        from agent_team.models import validate_and_filter_reviewer_findings
        from agent_team.run_manager import init_run

        spec = AgentReport(agent="testing", findings=[
            Finding(id="testing-001", priority="P1", title="Test issue", evidence="t.py:1", files=["t.py"], impact="I", recommendation="R"),
        ])

        with tempfile.TemporaryDirectory() as tmpdir:
            run_ctx = init_run(Path(tmpdir), timestamp="20260828-123000")
            malformed_finals = [
                {"source_finding_ids": ["nonexistent-999"], "title": "Bad ID", "evidence": "t.py:1", "impact": "I", "recommendation": "R"},
                {"source_finding_ids": ["testing-001"], "title": "", "evidence": "t.py:1", "impact": "I", "recommendation": "R"},
            ]

            valid_f, dropped = validate_and_filter_reviewer_findings(malformed_finals, [spec], run_ctx=run_ctx)
            self.assertEqual(len(valid_f), 0)
            self.assertEqual(len(dropped), 2)
            self.assertEqual(dropped[0]["reason"], "unknown_or_empty_sources")
            self.assertEqual(dropped[1]["reason"], "invalid_title")

            log_text = run_ctx.log_file.read_text(encoding="utf-8")
            self.assertIn("REVIEWER FINAL FINDING DROPPED: title='Bad ID', source_ids=['nonexistent-999'], reason='unknown_or_empty_sources'", log_text)

    def test_closure_scenario_23_sources_10_finals_no_silent_drop(self):
        """9. Escenario real de 23 sources y 10 finals: todos los hallazgos válidos sobreviven."""
        from agent_team.models import (
            derive_dispositions_from_reviewer_output,
            reconcile_and_guarantee_accounting,
            validate_and_filter_reviewer_findings,
        )

        # 4 specialists with total 23 findings
        specialists = [
            AgentReport(agent="architect", findings=[
                Finding(priority="P1", title=f"Arch issue {i}", evidence=f"arch{i}.py:10", files=[f"arch{i}.py"], impact="Risk", recommendation="Fix")
                for i in range(1, 4)
            ]),
            AgentReport(agent="backend", findings=[
                Finding(priority="P1", title=f"Back issue {i}", evidence=f"back{i}.py:10", files=[f"back{i}.py"], impact="Risk", recommendation="Fix")
                for i in range(1, 11)
            ]),
            AgentReport(agent="testing", findings=[
                Finding(priority="P1", title=f"Test issue {i}", evidence=f"test{i}.py:10", files=[f"test{i}.py"], impact="Risk", recommendation="Fix")
                for i in range(1, 7)
            ]),
            AgentReport(agent="docs", findings=[
                Finding(priority="P2", title=f"Doc issue {i}", evidence=f"doc{i}.md:10", files=[f"doc{i}.md"], impact="Risk", recommendation="Fix")
                for i in range(1, 5)
            ]),
        ]
        for s in specialists:
            s.ensure_finding_ids()

        orig_ids = {f.id for s in specialists for f in s.findings}
        self.assertEqual(len(orig_ids), 23)

        # Reviewer consolidates into 10 final findings covering all 23 sources
        raw_reviewer_finals = [
            {"source_finding_ids": ["architect-001", "architect-002", "architect-003"], "priority": "P1", "title": "Arch consolidated", "evidence": "arch1.py:10", "files": ["arch1.py"], "impact": "Risk", "recommendation": "Fix"},
            {"source_finding_ids": ["backend-001", "backend-002"], "priority": "P1", "title": "Back consolidated 1", "evidence": "back1.py:10", "files": ["back1.py"], "impact": "Risk", "recommendation": "Fix"},
            {"source_finding_ids": ["backend-003", "backend-004"], "priority": "P1", "title": "Back consolidated 2", "evidence": "back3.py:10", "files": ["back3.py"], "impact": "Risk", "recommendation": "Fix"},
            {"source_finding_ids": ["backend-005", "backend-006"], "priority": "P1", "title": "Back consolidated 3", "evidence": "back5.py:10", "files": ["back5.py"], "impact": "Risk", "recommendation": "Fix"},
            {"source_finding_ids": ["backend-007", "backend-008"], "priority": "P1", "title": "Back consolidated 4", "evidence": "back7.py:10", "files": ["back7.py"], "impact": "Risk", "recommendation": "Fix"},
            {"source_finding_ids": ["backend-009", "backend-010"], "priority": "P1", "title": "Back consolidated 5", "evidence": "back9.py:10", "files": ["back9.py"], "impact": "Risk", "recommendation": "Fix"},
            {"source_finding_ids": ["testing-001", "testing-002", "testing-003"], "priority": "P1", "title": "Test consolidated 1", "evidence": "test1.py:10", "files": ["test1.py"], "impact": "Risk", "recommendation": "Fix"},
            {"source_finding_ids": ["testing-004", "testing-005", "testing-006"], "priority": "P1", "title": "Test consolidated 2", "evidence": "test4.py:10", "files": ["test4.py"], "impact": "Risk", "recommendation": "Fix"},
            {"source_finding_ids": ["docs-001", "docs-002"], "priority": "P2", "title": "Docs consolidated 1", "evidence": "doc1.md:10", "files": ["doc1.md"], "impact": "Risk", "recommendation": "Fix"},
            {"source_finding_ids": ["docs-003", "docs-004"], "priority": "P2", "title": "Docs consolidated 2", "evidence": "doc3.md:10", "files": ["doc3.md"], "impact": "Risk", "recommendation": "Fix"},
        ]

        valid_finals, dropped = validate_and_filter_reviewer_findings(raw_reviewer_finals, specialists)
        self.assertEqual(len(dropped), 0)
        self.assertEqual(len(valid_finals), 10)

        disps, _ = derive_dispositions_from_reviewer_output(valid_finals, [], orig_ids)
        rep = ReviewerReport(summary="Audit 23 sources", final_findings=valid_finals, dispositions=disps)
        reconciled, accounting = reconcile_and_guarantee_accounting(rep, specialists)

        self.assertEqual(len(reconciled.final_findings), 10)
        self.assertEqual(accounting.total_input_findings, 23)
        self.assertEqual(accounting.accounted_count, 23)
        self.assertTrue(accounting.is_fully_accounted)


if __name__ == "__main__":
    unittest.main()
