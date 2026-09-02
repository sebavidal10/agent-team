import os
import tempfile
import unittest
from pathlib import Path

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
        bar0 = render_progress_bar(0, 4, width=4)
        self.assertEqual(bar0, "[░░░░]")

        bar2 = render_progress_bar(2, 4, width=4)
        self.assertEqual(bar2, "[██░░]")

        bar4 = render_progress_bar(4, 4, width=4)
        self.assertEqual(bar4, "[████]")

    def test_colors_and_no_color(self):
        c_enabled = Colors(enabled=True)
        self.assertIn("\033[32m", c_enabled.green("ok"))

        c_disabled = Colors(enabled=False)
        self.assertEqual(c_disabled.green("ok"), "ok")

    def test_dashboard_normal_and_compact_layouts(self):
        colors = Colors(enabled=False)
        observer = ConsoleObserver(colors=colors, is_tty=True)
        observer.start_pipeline("votacion-plataforma", "qwen2.5-coder:7b", 36)

        # Set mock agent row data
        observer.rows["profiler"].state = "Done"
        observer.rows["profiler"].time_seconds = 39.0
        observer.rows["profiler"].files_used = 12
        observer.rows["profiler"].files_candidates = 12
        observer.rows["profiler"].context_chars = 25000
        observer.rows["profiler"].findings_count = 0
        observer.rows["profiler"].output_status = "valid"
        observer.rows["profiler"].is_completed = True

        observer.rows["planner"].state = "Ollama"
        observer.rows["planner"].time_seconds = 24.0
        observer.rows["planner"].files_used = 18
        observer.rows["planner"].files_candidates = 79
        observer.rows["planner"].context_chars = 32000
        observer.rows["planner"].output_status = "..."
        observer.active_role = "planner"
        observer.active_phase_text = "Calling Ollama"

        # 1. Normal layout (width >= 80)
        lines_normal = observer._build_dashboard_lines(width=100)
        text_normal = "\n".join(lines_normal)
        self.assertIn("AGENT TEAM · votacion-plataforma · qwen2.5-coder:7b · READ-ONLY", text_normal)
        self.assertIn("AGENT", text_normal)
        self.assertIn("Profiler", text_normal)
        self.assertIn("Planner", text_normal)
        self.assertIn("Calling Ollama", text_normal)

        # 2. Compact layout (width < 80)
        lines_compact = observer._build_dashboard_lines(width=65)
        text_compact = "\n".join(lines_compact)
        self.assertIn("Profiler", text_compact)
        self.assertIn("Planner", text_compact)

    def test_console_observer_lifecycle_and_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "run.log"
            observer = ConsoleObserver(log_file=log_file, is_tty=False)

            observer.start_pipeline("test-repo", "qwen2.5-coder:7b", 15, num_ctx=16384)

            observer.start_agent("profiler", files_count=10, context_chars=12000, model="qwen2.5-coder:7b")
            observer.phase_start("profiler", "Preparing context")
            observer.phase_done("profiler", "Preparing context")
            observer.phase_start("profiler", "Calling Ollama...")
            observer.phase_done("profiler", "Ollama response received")

            observer.finish_agent("profiler", duration=14.5, findings_count=0, status="valid")

            observer.agent_warning("planner", "structured output invalid (retry 1/1)")
            observer.agent_failed("builder", "Timeout connecting to Ollama", duration=30.0)

            metrics = {
                "profiler": {"duration": 14.5, "files_count": 10, "status": "valid"},
                "planner": {"duration": 20.0, "files_count": 3, "status": "valid"},
                "builder": {"duration": 30.0, "files_count": 0, "status": "failed"},
                "reviewer": {"duration": 12.0, "files_count": 2, "status": "valid"},
            }
            observer.finish_pipeline(
                repo_name="test-repo",
                model="qwen2.5-coder:7b",
                total_duration=76.5,
                role_metrics=metrics,
                patches_count=2,
                review_status="approved",
                run_dir=Path(tmpdir) / "run-1",
                final_guide_path=Path(tmpdir) / "run-1" / "final-guide.md",
            )

            self.assertTrue(log_file.exists())
            log_content = log_file.read_text(encoding="utf-8")
            self.assertIn("RUN STARTED", log_content)
            self.assertIn("Ollama context window: 16384 tokens", log_content)
            self.assertIn("PROFILER", log_content)
            self.assertIn("Preparing context", log_content)
            self.assertIn("Ollama response received", log_content)
            self.assertIn("⚠ structured output invalid", log_content)
            self.assertIn("✗ BUILDER FAILED", log_content)
            self.assertIn("RUN COMPLETED", log_content)


if __name__ == "__main__":
    unittest.main()
