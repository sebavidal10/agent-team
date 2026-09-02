import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_team.config import Settings
from agent_team.graph import build_graph
from agent_team.models import (
    BuilderOutput,
    ImprovementItem,
    ImprovementPlan,
    PatchProposal,
    ProjectBlueprint,
    ReviewerOutput,
    extract_unified_diffs,
    format_blueprint_markdown,
    format_final_guide_markdown,
    format_improvement_plan_markdown,
    parse_builder_output,
    parse_improvement_plan,
    parse_project_blueprint,
    parse_reviewer_output,
    repair_json_string,
)
from agent_team.repo_context import (
    build_builder_snapshot,
    build_profiler_snapshot,
)
from agent_team.run_manager import (
    init_run,
    save_improvement_artifacts,
    save_manifest,
)


class TestImprovementTeam(unittest.TestCase):
    def test_repair_json_string(self):
        malformed = """
        Aquí tienes el blueprint:
        ```json
        {
            “project_name”: “mi-app”,
            “primary_language”: “TypeScript”,
            “framework”: “Next.js”,
            “key_libraries”: [“tailwind”, “prisma”,],
        }
        ```
        Espero que te sirva.
        """
        repaired = repair_json_string(malformed)
        data = json.loads(repaired)
        self.assertEqual(data["project_name"], "mi-app")
        self.assertEqual(data["primary_language"], "TypeScript")
        self.assertEqual(data["key_libraries"], ["tailwind", "prisma"])

    def test_project_blueprint_parsing_and_markdown(self):
        raw_json = json.dumps({
            "project_name": "ecommerce-api",
            "primary_language": "TypeScript",
            "framework": "FastAPI / Node",
            "key_libraries": ["prisma", "zod"],
            "architecture_style": "Clean Architecture",
            "code_conventions": ["Strict types", "Async/Await"],
            "test_setup": "Vitest en tests/",
            "summary": "API modular de comercio.",
        })
        bp = parse_project_blueprint(raw_json)
        self.assertEqual(bp.project_name, "ecommerce-api")
        self.assertEqual(bp.primary_language, "TypeScript")
        self.assertIn("zod", bp.key_libraries)

        md = format_blueprint_markdown(bp)
        self.assertIn("# Project Blueprint — ecommerce-api", md)
        self.assertIn("TypeScript", md)
        self.assertIn("`prisma`", md)

    def test_improvement_plan_parsing_and_markdown(self):
        raw = json.dumps({
            "goal": "Refactorizar autenticación",
            "summary": "Mejoras enfocadas en auth y sessions",
            "improvements": [
                {
                    "id": "IMP-01",
                    "title": "Añadir validación Zod en login",
                    "category": "Security",
                    "target_files": ["src/controllers/auth.ts"],
                    "rationale": "Prevenir inyecciones y payloads malformados",
                    "expected_impact": "Robustez en auth",
                    "implementation_steps": ["Definir schema", "Validar body"],
                }
            ],
        })
        plan = parse_improvement_plan(raw, goal="Refactorizar autenticación")
        self.assertEqual(len(plan.improvements), 1)
        self.assertEqual(plan.improvements[0].id, "IMP-01")
        self.assertEqual(plan.improvements[0].target_files, ["src/controllers/auth.ts"])

        md = format_improvement_plan_markdown(plan)
        self.assertIn("[IMP-01] Añadir validación Zod en login", md)
        self.assertIn("`src/controllers/auth.ts`", md)

    def test_patch_proposal_and_unified_diff_extraction(self):
        raw_markdown = """
        Aquí está el parche implementado:
        ```diff
        --- a/src/auth.ts
        +++ b/src/auth.ts
        @@ -10,4 +10,6 @@
         export function login() {
        +  validateInput();
         }
        ```
        """
        patches = extract_unified_diffs(raw_markdown)
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0].file_path, "src/auth.ts")
        self.assertIn("+  validateInput();", patches[0].diff_content)

    def test_reviewer_output_and_guide_formatting(self):
        patch = PatchProposal(
            improvement_id="IMP-01",
            title="Validación en Auth",
            file_path="src/auth.ts",
            action="modify",
            diff_content="--- a/src/auth.ts\n+++ b/src/auth.ts\n@@ -1,2 +1,3 @@\n+test();\n",
            explanation="Se agregó validación.",
        )
        rev = ReviewerOutput(
            overall_summary="Las mejoras son correctas.",
            review_status="approved",
            validated_patches=[patch],
            step_by_step_guide=["git apply output/run/patches/patch-01.diff"],
            verification_checklist=["Correr npm test"],
            warnings_or_notes=["Revisar env."],
        )
        guide_md = format_final_guide_markdown(rev)
        self.assertIn("# Guía de Aplicación de Mejoras", guide_md)
        self.assertIn("```diff", guide_md)
        self.assertIn("git apply", guide_md)
        self.assertIn("- [ ] Correr npm test", guide_md)

    def test_repo_context_snapshots(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "package.json").write_text('{"name": "test-app"}', encoding="utf-8")
            (root / "README.md").write_text("# Test App", encoding="utf-8")
            src_dir = root / "src"
            src_dir.mkdir()
            (src_dir / "index.ts").write_text("console.log('hello');", encoding="utf-8")

            # Profiler snapshot
            snap_p = build_profiler_snapshot(root)
            self.assertIn("package.json", snap_p.files_included)
            self.assertIn("README.md", snap_p.files_included)

            # Builder snapshot
            snap_b = build_builder_snapshot(root, target_files=["src/index.ts"])
            self.assertIn("src/index.ts", snap_b.files_included)
            self.assertIn("console.log('hello');", snap_b.content)

    @patch("agent_team.graph.ChatOllama")
    def test_graph_assembly_and_compilation(self, mock_chat_ollama):
        mock_chat_ollama.return_value = MagicMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "package.json").write_text('{"name": "test"}', encoding="utf-8")

            prompts_dir = Path(__file__).resolve().parents[1] / "agents"
            settings = Settings(
                model="qwen2.5-coder:7b",
                base_url="http://localhost:11434",
                num_ctx=16384,
                num_predict=4096,
                timeout_seconds=180,
                max_files=10,
                max_file_chars=1000,
                max_total_chars=10000,
                prompts_dir=prompts_dir,
                output_dir=root,
            )
            graph = build_graph(settings, root)
            self.assertIsNotNone(graph)

    def test_run_manager_artifacts_persistence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_ctx = init_run(root, timestamp="20260901-120000")
            self.assertTrue(run_ctx.patches_dir.exists())

            bp = ProjectBlueprint(
                project_name="demo-app",
                primary_language="TypeScript",
                framework="Next.js",
                key_libraries=["tailwind"],
                architecture_style="Modular",
                code_conventions=[],
                test_setup=None,
                summary="App de prueba",
            )
            plan = ImprovementPlan(
                goal="Optimizar",
                summary="Plan de prueba",
                improvements=[],
            )
            patch = PatchProposal(
                improvement_id="IMP-01",
                title="Test Patch",
                file_path="src/app.ts",
                action="modify",
                diff_content="--- a/src/app.ts\n+++ b/src/app.ts\n+console.log(1);\n",
                explanation="Log agregado",
            )
            b_out = BuilderOutput(summary="Done", patches=[patch])
            r_out = ReviewerOutput(
                overall_summary="Ok",
                review_status="approved",
                validated_patches=[patch],
                step_by_step_guide=["Step 1"],
                verification_checklist=[],
                warnings_or_notes=[],
            )

            save_improvement_artifacts(run_ctx, bp, plan, b_out, r_out)

            self.assertTrue(run_ctx.blueprint_file.exists())
            self.assertTrue(run_ctx.plan_file.exists())
            self.assertTrue(run_ctx.final_guide_file.exists())

            # Check individual patch file created
            patch_files = list(run_ctx.patches_dir.glob("*.diff"))
            self.assertEqual(len(patch_files), 1)
            self.assertIn("patch-01-src_app_ts.diff", patch_files[0].name)


if __name__ == "__main__":
    unittest.main()
