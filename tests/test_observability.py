import os
import tempfile
import unittest
from pathlib import Path

from agent_team.models import AgentReport, Finding, ReviewerReport
from agent_team.observability import (
    AgentRow,
    Colors,
    ConsoleObserver,
    format_chars_compact,
    format_duration_clock,
    format_duration_compact,
    format_files_compact,
    format_p0_p1_p2,
    render_progress_bar,
)


class TestObservability(unittest.TestCase):
    def test_duration_formatting(self):
        self.assertEqual(format_duration_clock(0), "00:00")
        self.assertEqual(format_duration_clock(35), "00:35")
        self.assertEqual(format_duration_clock(134), "02:14")
        self.assertEqual(format_duration_clock(3665), "61:05")

        self.assertEqual(format_duration_compact(0), "-")
        self.assertEqual(format_duration_compact(38.2), "38s")
        self.assertEqual(format_duration_compact(5.9), "6s")

    def test_chars_and_files_formatting(self):
        self.assertEqual(format_chars_compact(0), "-")
        self.assertEqual(format_chars_compact(950), "950")
        self.assertEqual(format_chars_compact(51508), "51.5k")
        self.assertEqual(format_chars_compact(1200000), "1.2M")

        self.assertEqual(format_files_compact(0, 0), "-")
        self.assertEqual(format_files_compact(18, 79), "18/79")
        self.assertEqual(format_files_compact(36, 36), "36/36")

        self.assertEqual(format_p0_p1_p2(0, 0, 3, has_run=True), "0/0/3")
        self.assertEqual(format_p0_p1_p2(1, 5, 6, has_run=True), "1/5/6")
        self.assertEqual(format_p0_p1_p2(0, 0, 0, has_run=False), "-")

    def test_progress_bar(self):
        bar0 = render_progress_bar(0, 6, width=6)
        self.assertEqual(bar0, "[░░░░░░]")

        bar2 = render_progress_bar(2, 6, width=6)
        self.assertEqual(bar2, "[██░░░░]")

        bar6 = render_progress_bar(6, 6, width=6)
        self.assertEqual(bar6, "[██████]")

    def test_colors_and_no_color(self):
        c_enabled = Colors(enabled=True)
        self.assertIn("\033[32m", c_enabled.green("ok"))

        c_disabled = Colors(enabled=False)
        self.assertEqual(c_disabled.green("ok"), "ok")
        self.assertEqual(c_disabled.red("error"), "error")
        self.assertEqual(c_disabled.cyan("running"), "running")

    def test_dashboard_normal_and_compact_layouts(self):
        colors = Colors(enabled=False)
        observer = ConsoleObserver(colors=colors, is_tty=True)
        observer.start_pipeline("votacion-plataforma", "qwen2.5-coder:7b", 36)

        # Set mock agent row data
        observer.rows["architect"].state = "Done"
        observer.rows["architect"].time_seconds = 39.0
        observer.rows["architect"].files_used = 36
        observer.rows["architect"].files_candidates = 36
        observer.rows["architect"].context_chars = 51508
        observer.rows["architect"].findings_count = 3
        observer.rows["architect"].p0 = 0
        observer.rows["architect"].p1 = 0
        observer.rows["architect"].p2 = 3
        observer.rows["architect"].output_status = "valid"
        observer.rows["architect"].is_completed = True

        observer.rows["backend"].state = "Ollama"
        observer.rows["backend"].time_seconds = 24.0
        observer.rows["backend"].files_used = 18
        observer.rows["backend"].files_candidates = 79
        observer.rows["backend"].context_chars = 57600
        observer.rows["backend"].output_status = "..."
        observer.active_role = "backend"
        observer.active_phase_text = "Calling Ollama"

        # 1. Normal layout (width >= 80)
        lines_normal = observer._build_dashboard_lines(width=100)
        text_normal = "\n".join(lines_normal)
        self.assertIn("AGENT TEAM · votacion-plataforma · qwen2.5-coder:7b · READ-ONLY", text_normal)
        self.assertIn("AGENT", text_normal)
        self.assertIn("STATE", text_normal)
        self.assertIn("FILES", text_normal)
        self.assertIn("CTX", text_normal)
        self.assertIn("P0/P1/P2", text_normal)
        self.assertIn("Architect", text_normal)
        self.assertIn("36/36", text_normal)
        self.assertIn("51.5k", text_normal)
        self.assertIn("0/0/3", text_normal)
        self.assertIn("Backend", text_normal)
        self.assertIn("18/79", text_normal)
        self.assertIn("57.6k", text_normal)
        self.assertIn("Calling Ollama", text_normal)

        # 2. Compact layout (width < 80)
        lines_compact = observer._build_dashboard_lines(width=65)
        text_compact = "\n".join(lines_compact)
        self.assertIn("P0/1/2", text_compact)
        self.assertIn("Architect", text_compact)
        self.assertIn("51.5k", text_compact)
        self.assertIn("0/0/3", text_compact)

    def test_console_observer_lifecycle_and_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "run.log"
            observer = ConsoleObserver(log_file=log_file, is_tty=False)

            observer.start_pipeline("test-repo", "qwen2.5-coder:7b", 15, num_ctx=16384)

            observer.start_agent("architect", files_count=10, context_chars=12000, model="qwen2.5-coder:7b")
            observer.phase_start("architect", "Preparing context")
            observer.phase_done("architect", "Preparing context")
            observer.phase_start("architect", "Calling Ollama...")
            observer.phase_done("architect", "Ollama response received")

            rep = AgentReport(
                agent="architect",
                summary="Architecture fine.",
                findings=[
                    Finding(
                        priority="P0",
                        title="Critical flaw",
                        evidence="auth.py",
                        impact="High",
                        recommendation="Fix",
                        confidence="high",
                    ),
                    Finding(
                        priority="P1",
                        title="Missing route",
                        evidence="routes.py",
                        impact="Med",
                        recommendation="Add route",
                        confidence="medium",
                    ),
                ],
            )
            observer.complete_agent("architect", rep, duration=14.5, files_count=10, context_chars=12000)

            observer.agent_warning("backend", "structured output invalid (retry 1/1)")
            observer.agent_failed("testing", "Timeout connecting to Ollama", duration=30.0)

            rev_rep = ReviewerReport(
                summary="Audit completed with 1 P0.",
                release_blockers=[rep.findings[0]],
                p0=[rep.findings[0]],
                p1=[rep.findings[1]],
                p2=[],
                deduplicated_findings=rep.findings,
            )
            metrics = {
                "architect": {"duration_seconds": 14.5, "findings": 2, "structured_output": "valid", "retries": 0},
                "backend": {"duration_seconds": 20.0, "findings": 0, "structured_output": "repaired", "retries": 1},
                "frontend": {"duration_seconds": 15.0, "findings": 0, "structured_output": "valid", "retries": 0},
                "testing": {"duration_seconds": 30.0, "findings": 0, "structured_output": "failed", "retries": 0},
                "docs": {"duration_seconds": 10.0, "findings": 0, "structured_output": "valid", "retries": 0},
                "reviewer": {"duration_seconds": 12.0, "findings": 2, "structured_output": "valid", "retries": 0},
            }
            observer.finish_pipeline(
                repo_name="test-repo",
                model="qwen2.5-coder:7b",
                total_duration=101.5,
                role_metrics=metrics,
                reviewer_report=rev_rep,
                run_dir=Path(tmpdir) / "run-1",
                final_report_path=Path(tmpdir) / "run-1" / "final-report.md",
            )

            self.assertTrue(log_file.exists())
            log_content = log_file.read_text(encoding="utf-8")
            self.assertIn("RUN STARTED", log_content)
            self.assertIn("Ollama context window: 16384 tokens", log_content)
            self.assertIn("ARCHITECT", log_content)
            self.assertIn("Preparing context", log_content)
            self.assertIn("Ollama response received", log_content)
            self.assertIn("✓ ARCHITECT completed", log_content)
            self.assertIn("⚠ structured output invalid", log_content)
            self.assertIn("✗ TESTING FAILED", log_content)
            self.assertIn("RUN COMPLETED", log_content)


if __name__ == "__main__":
    unittest.main()
