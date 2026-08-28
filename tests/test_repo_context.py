import tempfile
import unittest
from pathlib import Path

from agent_team.repo_context import (
    IGNORE_DIRS,
    IMPORTANT_NAMES,
    ROLE_CHAR_LIMITS,
    RepoSnapshot,
    _is_candidate,
    _matches_role,
    _priority,
    build_role_snapshots,
    build_snapshot,
)


class TestRepoContext(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        # Create dummy directory structure
        (self.root / "src").mkdir()
        (self.root / "src" / "routes").mkdir()
        (self.root / "src" / "components").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "docs").mkdir()
        (self.root / "node_modules").mkdir()

        # Create dummy files
        (self.root / "README.md").write_text("# Project", encoding="utf-8")
        (self.root / "package.json").write_text('{"name": "test"}', encoding="utf-8")
        (self.root / "src" / "routes" / "api.py").write_text("def get_users(): pass", encoding="utf-8")
        (self.root / "src" / "components" / "Button.tsx").write_text("export const Button = () => null;", encoding="utf-8")
        (self.root / "tests" / "test_api.py").write_text("def test_users(): assert True", encoding="utf-8")
        (self.root / "docs" / "architecture.md").write_text("# Architecture spec", encoding="utf-8")
        (self.root / "node_modules" / "ignored.js").write_text("console.log('ignore');", encoding="utf-8")
        (self.root / "binary.bin").write_bytes(b"\x00\x01\x02")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_is_candidate(self):
        self.assertTrue(_is_candidate(Path("README.md")))
        self.assertTrue(_is_candidate(Path("test.py")))
        self.assertTrue(_is_candidate(Path("styles.css")))
        self.assertTrue(_is_candidate(Path(".env.local")))
        self.assertFalse(_is_candidate(Path("binary.bin")))
        self.assertFalse(_is_candidate(Path(".env")))

    def test_matches_role(self):
        api_file = self.root / "src" / "routes" / "api.py"
        btn_file = self.root / "src" / "components" / "Button.tsx"
        test_file = self.root / "tests" / "test_api.py"
        doc_file = self.root / "docs" / "architecture.md"

        self.assertTrue(_matches_role(api_file, self.root, "backend"))
        self.assertTrue(_matches_role(btn_file, self.root, "frontend"))
        self.assertTrue(_matches_role(test_file, self.root, "testing"))
        self.assertTrue(_matches_role(doc_file, self.root, "docs"))
        self.assertTrue(_matches_role(doc_file, self.root, "architect"))

    def test_build_snapshot(self):
        snapshot = build_snapshot(
            root=self.root,
            max_files=10,
            max_file_chars=1000,
            max_total_chars=10000,
            role="backend",
        )
        self.assertIsInstance(snapshot, RepoSnapshot)
        self.assertIn("package.json", snapshot.files_included)
        self.assertIn("src/routes/api.py", snapshot.files_included)
        self.assertNotIn("node_modules/ignored.js", snapshot.files_included)

    def test_build_role_snapshots_and_limits(self):
        snapshots = build_role_snapshots(
            root=self.root,
            max_files=10,
            max_file_chars=1000,
        )
        expected_roles = {"architect", "backend", "frontend", "testing", "docs"}
        self.assertEqual(set(snapshots.keys()), expected_roles)
        for role, snap in snapshots.items():
            self.assertIsInstance(snap, RepoSnapshot)
            self.assertGreaterEqual(len(snap.files_included), 1)
            self.assertGreaterEqual(snap.candidates_total, len(snap.files_included))
            self.assertEqual(snap.candidates_discarded, snap.candidates_total - len(snap.files_included))

    def test_budget_exceeded_discards_candidates(self):
        # Set character budget so only 1 file fits
        snapshot = build_snapshot(
            root=self.root,
            max_files=10,
            max_file_chars=1000,
            max_total_chars=100,
            role="backend",
        )
        self.assertEqual(len(snapshot.files_included), 1)
        self.assertGreater(snapshot.candidates_discarded, 0)

    def test_repo_context_excludes_lockfiles(self):
        """1. repo_context no consume package-lock en content."""
        (self.root / "package-lock.json").write_text('{"lockfileVersion": 3, "data": "huge"}', encoding="utf-8")
        (self.root / "yarn.lock").write_text('# yarn lockfile', encoding="utf-8")
        
        self.assertFalse(_is_candidate(self.root / "package-lock.json"))
        self.assertFalse(_is_candidate(self.root / "yarn.lock"))

        snap = build_snapshot(root=self.root, role="frontend")
        self.assertNotIn("package-lock.json", snap.files_included)
        self.assertNotIn("yarn.lock", snap.files_included)

    def test_overflow_file_does_not_stop_subsequent_smaller_candidates(self):
        """2. Un archivo que no cabe no impide considerar candidatos posteriores."""
        # Create a directory with a huge candidate file and a small candidate file
        huge_file = self.root / "src" / "routes" / "huge_controller.py"
        small_file = self.root / "src" / "routes" / "small_controller.py"
        huge_file.write_text("x" * 5000, encoding="utf-8")
        small_file.write_text("y" * 100, encoding="utf-8")

        # Budget is 1500 chars (huge doesn't fit, but small fits!)
        snap = build_snapshot(
            root=self.root,
            max_files=10,
            max_file_chars=10000,
            max_total_chars=1500,
            role="backend",
        )
        self.assertNotIn("src/routes/huge_controller.py", snap.files_included)
        self.assertIn("src/routes/small_controller.py", snap.files_included)

    def test_frontend_prioritizes_components_over_tests(self):
        """3. Frontend prioriza implementation antes que *.test.*."""
        component_file = self.root / "src" / "components" / "Modal.tsx"
        test_file = self.root / "src" / "components" / "Modal.test.tsx"
        component_file.write_text("export const Modal = () => null;", encoding="utf-8")
        test_file.write_text("test('modal', () => {});", encoding="utf-8")

        p_comp = _priority(component_file, self.root, "frontend")
        p_test = _priority(test_file, self.root, "frontend")

        # Lower score = higher priority
        self.assertLess(p_comp[0], p_test[0])

    def test_backend_prioritizes_implementation_over_tests(self):
        """4. Backend prioriza implementation antes que tests equivalentes."""
        controller_file = self.root / "src" / "routes" / "user.controller.ts"
        test_file = self.root / "src" / "routes" / "user.controller.test.ts"
        controller_file.write_text("class UserController {}", encoding="utf-8")
        test_file.write_text("describe('UserController', () => {});", encoding="utf-8")

        p_ctrl = _priority(controller_file, self.root, "backend")
        p_test = _priority(test_file, self.root, "backend")

        self.assertLess(p_ctrl[0], p_test[0])

    def test_testing_role_includes_both_production_and_test_files(self):
        """5. Testing conoce inventario productivo además de tests."""
        snap = build_snapshot(root=self.root, role="testing")
        # Testing snapshot tree contains both test files and key src files
        self.assertIn("tests/test_api.py", snap.tree)
        self.assertIn("src/routes/api.py", snap.tree)
        self.assertIn("src/components/Button.tsx", snap.tree)

    def test_docs_prioritizes_env_example(self):
        """7. Docs prioriza .env.example."""
        env_file = self.root / ".env.example"
        env_file.write_text("DATABASE_URL=postgres://...", encoding="utf-8")
        random_file = self.root / "docs" / "misc.md"
        random_file.write_text("# Misc docs", encoding="utf-8")

        p_env = _priority(env_file, self.root, "docs")
        p_misc = _priority(random_file, self.root, "docs")

        # Lower score = higher priority
        self.assertLessEqual(p_env[0], p_misc[0])

        snap = build_snapshot(root=self.root, role="docs")
        self.assertIn(".env.example", snap.files_included)

    def test_reviewer_evidence_context_contains_source_findings_files(self):
        """12 & 13. Reviewer evidence context contiene archivos de source findings y no 36 innecesarios."""
        from agent_team.repo_context import build_targeted_snapshot

        # Suppose specialists cited 2 files
        target_files = {"src/routes/api.py", ".env.example"}
        (self.root / ".env.example").write_text("KEY=val", encoding="utf-8")

        targeted_snap = build_targeted_snapshot(
            root=self.root,
            target_files=target_files,
            full_inventory_tree="tree listing",
            max_file_chars=5000,
            max_total_chars=20000,
        )

        self.assertEqual(len(targeted_snap.files_included), 2)
        self.assertIn("src/routes/api.py", targeted_snap.files_included)
        self.assertIn(".env.example", targeted_snap.files_included)
        self.assertNotIn("README.md", targeted_snap.files_included)
        self.assertNotIn("src/components/Button.tsx", targeted_snap.files_included)


if __name__ == "__main__":
    unittest.main()
