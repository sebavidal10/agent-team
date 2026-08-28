from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from .models import AgentReport, Finding, ReviewerReport
from .repo_context import RepoSnapshot


def _should_use_color() -> bool:
    if "NO_COLOR" in os.environ:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return sys.stdout.isatty()


class Colors:
    def __init__(self, enabled: bool | None = None):
        self.enabled = _should_use_color() if enabled is None else enabled

    def _c(self, code: str, text: str) -> str:
        if not self.enabled:
            return text
        return f"\033[{code}m{text}\033[0m"

    def green(self, text: str) -> str:
        return self._c("32", text)

    def cyan(self, text: str) -> str:
        return self._c("36", text)

    def blue(self, text: str) -> str:
        return self._c("34", text)

    def yellow(self, text: str) -> str:
        return self._c("33", text)

    def red(self, text: str) -> str:
        return self._c("31", text)

    def gray(self, text: str) -> str:
        return self._c("90", text)

    def bold(self, text: str) -> str:
        return self._c("1", text)


def format_duration_clock(seconds: float) -> str:
    total_sec = max(0, int(seconds))
    mins, secs = divmod(total_sec, 60)
    return f"{mins:02d}:{secs:02d}"


def format_duration_compact(seconds: float) -> str:
    if seconds <= 0:
        return "-"
    return f"{int(round(seconds))}s"


def format_chars_compact(chars: int) -> str:
    if chars <= 0:
        return "-"
    if chars >= 1_000_000:
        return f"{chars / 1_000_000:.1f}M"
    if chars >= 1_000:
        return f"{chars / 1_000:.1f}k"
    return str(chars)


def format_files_compact(used: int, candidates: int) -> str:
    if used <= 0 and candidates <= 0:
        return "-"
    if candidates > 0:
        return f"{used}/{candidates}"
    return str(used)


def format_p0_p1_p2(p0: int, p1: int, p2: int, has_run: bool) -> str:
    if not has_run:
        return "-"
    return f"{p0}/{p1}/{p2}"


def render_progress_bar(completed: int, total: int, width: int = 16) -> str:
    if total <= 0:
        return f"[{'░' * width}] 0/0"
    completed = max(0, min(completed, total))
    filled = int((completed / total) * width)
    empty = width - filled
    return f"[{'█' * filled}{'░' * empty}]"


ANSI_REGEX = re.compile(r"\033\[[0-9;]*m")


def visible_len(s: str) -> int:
    return len(ANSI_REGEX.sub("", s))


def pad_ansi(s: str, width: int, align: str = "left") -> str:
    v_len = visible_len(s)
    pad = max(0, width - v_len)
    if align == "right":
        return (" " * pad) + s
    elif align == "center":
        left = pad // 2
        right = pad - left
        return (" " * left) + s + (" " * right)
    return s + (" " * pad)


@dataclass
class AgentRow:
    role: str
    state: str = "Waiting"  # Waiting, Preparing, Ollama, Parsing, Saving, Done, Warning, Failed
    time_seconds: float = 0.0
    files_used: int = 0
    files_candidates: int = 0
    context_chars: int = 0
    findings_count: int = 0
    p0: int = 0
    p1: int = 0
    p2: int = 0
    output_status: str = "-"  # -, ..., valid, repaired, fallback, failed
    retries: int = 0
    start_time: float = 0.0
    is_completed: bool = False
    is_failed: bool = False


class ConsoleObserver:
    FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    ROLES = ("architect", "backend", "frontend", "testing", "docs", "reviewer")

    def __init__(
        self,
        log_file: Path | None = None,
        colors: Colors | None = None,
        is_tty: bool | None = None,
    ):
        self.log_file = log_file
        self.colors = colors or Colors()
        self.is_tty = sys.stdout.isatty() if is_tty is None else is_tty
        self.repo_name = ""
        self.model = ""
        self.total_unique_files = 0
        self.run_start_time = time.time()

        self.rows: dict[str, AgentRow] = {
            r: AgentRow(role=r) for r in self.ROLES
        }

        self.active_role: str | None = None
        self.active_phase_text: str = ""
        self.active_retry: int = 0

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._bg_thread: threading.Thread | None = None
        self._rendered_lines_count = 0
        self._frame_idx = 0

    def _write_log(self, text: str) -> None:
        if not self.log_file:
            return
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError:
            pass

    def start_pipeline(
        self,
        repo_name: str,
        model: str,
        total_unique_files: int,
        snapshots: dict[str, RepoSnapshot] | None = None,
        num_ctx: int | None = None,
    ) -> None:
        self.repo_name = repo_name
        self.model = model
        self.total_unique_files = total_unique_files
        self.run_start_time = time.time()

        if snapshots:
            for role, snap in snapshots.items():
                if role in self.ROLES:
                    self.rows[role].files_used = len(snap.files_included)
                    self.rows[role].files_candidates = snap.candidates_total or len(snap.files_included)
                    self.rows[role].context_chars = snap.total_chars

        self._write_log(f"--- RUN STARTED: {datetime.now().isoformat()} ---")
        self._write_log(f"Repository: {repo_name} | Model: {model} | Unique files: {total_unique_files}")
        if num_ctx:
            self._write_log(f"Ollama context window: {num_ctx} tokens")

        if self.is_tty:
            self._render_dashboard()
            self._start_refresh_thread()
        else:
            print(f"AGENT TEAM · {repo_name} · {model} · READ-ONLY")
            print(f"Archivos únicos: {total_unique_files}")

    def _start_refresh_thread(self) -> None:
        if not self.is_tty or (self._bg_thread and self._bg_thread.is_alive()):
            return
        self._stop_event.clear()
        self._bg_thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self._bg_thread.start()

    def _stop_refresh_thread(self) -> None:
        if self._bg_thread and self._bg_thread.is_alive():
            self._stop_event.set()
            self._bg_thread.join(timeout=0.5)
            self._bg_thread = None

    def _refresh_loop(self) -> None:
        while not self._stop_event.is_set():
            time.sleep(1.0)
            with self._lock:
                if self.active_role and not self._stop_event.is_set():
                    row = self.rows.get(self.active_role)
                    if row and not row.is_completed and not row.is_failed:
                        row.time_seconds = time.time() - row.start_time
                    self._frame_idx += 1
                    self._render_dashboard()

    def start_agent(
        self,
        role: str,
        files_count: int,
        context_chars: int,
        model: str,
        discarded_files: int = 0,
        candidates_total: int | None = None,
    ) -> None:
        with self._lock:
            self.active_role = role
            self.active_phase_text = "Preparing context"
            self.active_retry = 0
            row = self.rows.get(role)
            if row:
                row.state = "Preparing"
                row.start_time = time.time()
                row.files_used = files_count
                row.files_candidates = candidates_total if candidates_total else (files_count + discarded_files)
                row.context_chars = context_chars
                row.output_status = "..."
                row.time_seconds = 0.0

            if self.is_tty:
                self._render_dashboard()
            else:
                now_str = datetime.now().strftime("%H:%M:%S")
                print(f"[{now_str}] ▶ {role.upper()} (files: {files_count}/{row.files_candidates if row else files_count}, ctx: {context_chars} chars)")

        now_str = datetime.now().strftime("%H:%M:%S")
        self._write_log(f"[{now_str}] ▶ {role.upper()} started (files: {files_count}, ctx: {context_chars})")

    def phase_start(self, role: str, phase_name: str) -> None:
        with self._lock:
            self.active_role = role
            self.active_phase_text = phase_name
            row = self.rows.get(role)
            if row:
                if "Ollama" in phase_name:
                    row.state = "Ollama"
                elif "Parsing" in phase_name or "consolidating" in phase_name:
                    row.state = "Parsing"
                elif "Saving" in phase_name:
                    row.state = "Saving"
                elif "Preparing" in phase_name:
                    row.state = "Preparing"
                elif "Retrying" in phase_name:
                    row.state = "Ollama"
                    self.active_retry = 1
                row.time_seconds = time.time() - row.start_time

            if self.is_tty:
                self._render_dashboard()

        now_str = datetime.now().strftime("%H:%M:%S")
        self._write_log(f"  [{now_str}]   ▶ {phase_name}")

    def phase_done(self, role: str, phase_name: str, note: str | None = None) -> None:
        with self._lock:
            row = self.rows.get(role)
            if row and not row.is_completed:
                row.time_seconds = time.time() - row.start_time

            if self.is_tty:
                self._render_dashboard()

        now_str = datetime.now().strftime("%H:%M:%S")
        note_str = f" ({note})" if note else ""
        self._write_log(f"  [{now_str}]   ✓ {phase_name}{note_str}")

    def agent_warning(self, role: str, message: str) -> None:
        with self._lock:
            self.active_retry = 1
            row = self.rows.get(role)
            if row:
                row.retries = 1
            if self.is_tty:
                self._render_dashboard()
            else:
                now_str = datetime.now().strftime("%H:%M:%S")
                print(f"            ⚠ {role.upper()}: {message}")

        now_str = datetime.now().strftime("%H:%M:%S")
        self._write_log(f"  [{now_str}]   ⚠ {message}")

    def agent_failed(self, role: str, reason: str, duration: float) -> None:
        with self._lock:
            row = self.rows.get(role)
            if row:
                row.state = "Failed"
                row.time_seconds = duration
                row.output_status = "failed"
                row.is_failed = True
            if self.active_role == role:
                self.active_role = None

            if self.is_tty:
                self._render_dashboard()
            else:
                now_str = datetime.now().strftime("%H:%M:%S")
                print(f"[{now_str}] ✗ {role.upper()} FAILED ({duration:.1f}s): {reason}")

        now_str = datetime.now().strftime("%H:%M:%S")
        self._write_log(f"[{now_str}] ✗ {role.upper()} FAILED (duration: {duration:.1f}s, reason: {reason})")

    def complete_agent(
        self,
        role: str,
        report: AgentReport | ReviewerReport,
        duration: float,
        files_count: int,
        context_chars: int,
        discarded_files: int = 0,
        candidates_total: int | None = None,
    ) -> None:
        with self._lock:
            row = self.rows.get(role)
            if isinstance(report, ReviewerReport):
                findings_list = report.final_findings
                p0 = len(report.p0)
                p1 = len(report.p1)
                p2 = len(report.p2)
            else:
                findings_list = report.findings
                p0 = sum(1 for f in findings_list if f.priority == "P0")
                p1 = sum(1 for f in findings_list if f.priority == "P1")
                p2 = sum(1 for f in findings_list if f.priority in {"P2", "P3"})

            if row:
                row.state = "Done"
                row.time_seconds = duration
                row.files_used = files_count
                row.files_candidates = candidates_total if candidates_total else (files_count + discarded_files)
                row.context_chars = context_chars
                row.findings_count = len(findings_list)
                row.p0 = p0
                row.p1 = p1
                row.p2 = p2
                row.output_status = report.status
                row.retries = report.retries
                row.is_completed = True

            if self.active_role == role:
                self.active_role = None

            if self.is_tty:
                self._render_dashboard()
            else:
                now_str = datetime.now().strftime("%H:%M:%S")
                print(
                    f"[{now_str}] ✓ {role.upper():<10} Done  {duration:.0f}s  "
                    f"{files_count}/{row.files_candidates if row else files_count}  "
                    f"{len(findings_list)} findings ({p0}/{p1}/{p2})  [{report.status}]"
                )

        now_str = datetime.now().strftime("%H:%M:%S")
        self._write_log(
            f"[{now_str}] ✓ {role.upper()} completed (duration: {duration:.1f}s, findings: {len(findings_list)}, "
            f"output: {report.status}, retries: {report.retries}, P0:{p0} P1:{p1} P2:{p2})"
        )

    def _format_state_cell(self, row: AgentRow, compact: bool = False) -> str:
        st = row.state
        if st == "Waiting":
            sym = self.colors.gray("○")
            label = "Wait" if compact else "Waiting"
            return f"{sym} {self.colors.gray(label)}"
        elif st == "Done":
            sym = self.colors.green("✓")
            label = "Done"
            return f"{sym} {self.colors.green(label)}"
        elif st == "Failed":
            sym = self.colors.red("✗")
            label = "Fail" if compact else "Failed"
            return f"{sym} {self.colors.red(label)}"
        elif st == "Warning":
            sym = self.colors.yellow("⚠")
            label = "Warn" if compact else "Warning"
            return f"{sym} {self.colors.yellow(label)}"
        else:
            # Active states: Preparing, Ollama, Parsing, Saving
            sym = self.colors.cyan("▶")
            label = st
            return f"{sym} {self.colors.cyan(label)}"

    def _format_output_cell(self, status: str) -> str:
        if status == "valid":
            return self.colors.green("valid")
        if status in {"repaired", "fallback"}:
            return self.colors.yellow(status)
        if status == "failed":
            return self.colors.red("failed")
        if status == "...":
            return self.colors.cyan("...")
        return self.colors.gray("-")

    def _build_dashboard_lines(self, width: int) -> list[str]:
        now = time.time()
        elapsed_clock = format_duration_clock(now - self.run_start_time)
        completed_count = sum(1 for r in self.rows.values() if r.is_completed)
        total_roles = len(self.ROLES)
        progress_bar = render_progress_bar(completed_count, total_roles, width=16)

        is_compact = width < 80

        # Header lines
        line1 = (
            f"{self.colors.bold('AGENT TEAM')} · "
            f"{self.colors.cyan(self.repo_name)} · "
            f"{self.model} · "
            f"{self.colors.gray('READ-ONLY')}"
        )
        line2 = (
            f"Progress {completed_count}/{total_roles} {self.colors.cyan(progress_bar)} · "
            f"Elapsed {self.colors.bold(elapsed_clock)}"
        )

        lines = [line1, line2, ""]

        if is_compact:
            # Compact Layout
            hdr = (
                f"{pad_ansi('AGENT', 10)} "
                f"{pad_ansi('STATE', 9)} "
                f"{pad_ansi('TIME', 5, 'right')} "
                f"{pad_ansi('FILES', 7, 'right')} "
                f"{pad_ansi('CTX', 6, 'right')} "
                f"{pad_ansi('FIND', 4, 'right')} "
                f"{pad_ansi('P0/1/2', 7, 'center')} "
                f"{pad_ansi('OUTPUT', 8)}"
            )
            lines.append(self.colors.bold(hdr))
            for role in self.ROLES:
                row = self.rows[role]
                agent_name = role.capitalize()
                c_agent = pad_ansi(agent_name, 10)
                c_state = pad_ansi(self._format_state_cell(row, compact=True), 9)
                c_time = pad_ansi(format_duration_compact(row.time_seconds), 5, "right")
                c_files = pad_ansi(format_files_compact(row.files_used, row.files_candidates), 7, "right")
                c_ctx = pad_ansi(format_chars_compact(row.context_chars), 6, "right")
                find_val = str(row.findings_count) if (row.is_completed or row.is_failed) else "-"
                c_find = pad_ansi(find_val, 4, "right")
                c_p = pad_ansi(format_p0_p1_p2(row.p0, row.p1, row.p2, row.is_completed), 7, "center")
                c_out = pad_ansi(self._format_output_cell(row.output_status), 8)

                lines.append(f"{c_agent} {c_state} {c_time} {c_files} {c_ctx} {c_find} {c_p} {c_out}")
        else:
            # Normal Layout
            hdr = (
                f"{pad_ansi('AGENT', 11)} "
                f"{pad_ansi('STATE', 11)} "
                f"{pad_ansi('TIME', 6, 'right')}   "
                f"{pad_ansi('FILES', 7, 'right')}   "
                f"{pad_ansi('CTX', 7, 'right')}   "
                f"{pad_ansi('FIND', 4, 'right')}   "
                f"{pad_ansi('P0/P1/P2', 8, 'center')}   "
                f"{pad_ansi('OUTPUT', 8)}"
            )
            lines.append(self.colors.bold(hdr))
            for role in self.ROLES:
                row = self.rows[role]
                agent_name = role.capitalize()
                c_agent = pad_ansi(agent_name, 11)
                c_state = pad_ansi(self._format_state_cell(row, compact=False), 11)
                c_time = pad_ansi(format_duration_compact(row.time_seconds), 6, "right")
                c_files = pad_ansi(format_files_compact(row.files_used, row.files_candidates), 7, "right")
                c_ctx = pad_ansi(format_chars_compact(row.context_chars), 7, "right")
                find_val = str(row.findings_count) if (row.is_completed or row.is_failed) else "-"
                c_find = pad_ansi(find_val, 4, "right")
                c_p = pad_ansi(format_p0_p1_p2(row.p0, row.p1, row.p2, row.is_completed), 8, "center")
                c_out = pad_ansi(self._format_output_cell(row.output_status), 8)

                lines.append(f"{c_agent} {c_state} {c_time}   {c_files}   {c_ctx}   {c_find}   {c_p}   {c_out}")

        lines.append("")

        # Active agent status line (bottom)
        if self.active_role:
            active_row = self.rows.get(self.active_role)
            frame = self.FRAMES[self._frame_idx % len(self.FRAMES)]
            agent_elapsed = format_duration_clock(now - active_row.start_time) if active_row else "00:00"
            retry_tag = f" · retry {self.active_retry}/1" if self.active_retry > 0 else ""
            active_line = (
                f"{self.colors.cyan(frame)} "
                f"{self.colors.bold(self.active_role.capitalize())} · "
                f"{self.active_phase_text} · "
                f"{self.colors.cyan(agent_elapsed)}{retry_tag} · "
                f"{self.colors.gray(f'total {elapsed_clock}')}"
            )
            lines.append(active_line)
        else:
            lines.append("")

        return lines

    def _render_dashboard(self) -> None:
        if not self.is_tty:
            return

        term_width = shutil.get_terminal_size((80, 24)).columns
        lines = self._build_dashboard_lines(term_width)

        # In-place rewrite: move cursor up by previous line count
        if self._rendered_lines_count > 0:
            sys.stdout.write(f"\033[{self._rendered_lines_count}A\r")

        for line in lines:
            sys.stdout.write(f"\033[K{line}\n")
        sys.stdout.flush()

        self._rendered_lines_count = len(lines)

    def finish_pipeline(
        self,
        repo_name: str,
        model: str,
        total_duration: float,
        role_metrics: dict[str, dict[str, Any]],
        reviewer_report: ReviewerReport | None,
        run_dir: Path,
        final_report_path: Path,
    ) -> None:
        self._stop_refresh_thread()

        with self._lock:
            self.active_role = None

            # Final metrics update
            for role, m in role_metrics.items():
                if role in self.rows:
                    row = self.rows[role]
                    row.time_seconds = m.get("duration_seconds", row.time_seconds)
                    row.findings_count = m.get("findings", row.findings_count)
                    row.output_status = m.get("structured_output", row.output_status)
                    row.retries = m.get("retries", row.retries)
                    row.is_completed = True
                    row.state = "Done"
                    if role == "reviewer" and reviewer_report:
                        row.p0 = len(reviewer_report.p0)
                        row.p1 = len(reviewer_report.p1)
                        row.p2 = len(reviewer_report.p2)

            if self.is_tty:
                # Render final table with active line cleared
                self._render_dashboard()

        clock_str = format_duration_clock(total_duration)
        findings_total = len(reviewer_report.final_findings) if reviewer_report else sum(r.findings_count for r in self.rows.values())
        p0 = len(reviewer_report.p0) if reviewer_report else sum(r.p0 for r in self.rows.values())
        p1 = len(reviewer_report.p1) if reviewer_report else sum(r.p1 for r in self.rows.values())
        p2 = len(reviewer_report.p2) if reviewer_report else sum(r.p2 for r in self.rows.values())

        # Concise summary lines below the final table
        summary_lines = [
            f"{self.colors.green('✓')} {self.colors.bold('Audit complete')} · {self.colors.bold(clock_str)}",
            f"{self.colors.bold(str(findings_total))} findings · {self.colors.red('P0')}:{p0}  {self.colors.yellow('P1')}:{p1}  {self.colors.gray('P2')}:{p2}",
            f"Report: {final_report_path}",
            "",
        ]

        for s in summary_lines:
            print(s)

        self._write_log(
            f"--- RUN COMPLETED: {datetime.now().isoformat()} (duration: {total_duration:.1f}s, "
            f"findings: {findings_total}, P0:{p0} P1:{p1} P2:{p2}) ---"
        )
        self._write_log(f"Final Report: {final_report_path}")
