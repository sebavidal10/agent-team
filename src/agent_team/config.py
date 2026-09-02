from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    model: str
    base_url: str
    num_ctx: int
    num_predict: int
    timeout_seconds: int
    max_files: int
    max_file_chars: int
    max_total_chars: int
    prompts_dir: Path
    output_dir: Path
    reviewer_model: str = ""
    interactive: bool = False

    def get_reviewer_model(self) -> str:
        return self.reviewer_model or self.model


def load_settings() -> Settings:
    load_dotenv()

    root = Path(__file__).resolve().parents[2]

    base_model = os.getenv("AGENT_MODEL", "qwen2.5-coder:7b")
    reviewer_model = os.getenv("REVIEWER_MODEL", base_model)

    return Settings(
        model=base_model,
        base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        num_ctx=int(os.getenv("OLLAMA_NUM_CTX", "16384")),
        num_predict=int(os.getenv("OLLAMA_NUM_PREDICT", "4096")),
        timeout_seconds=int(os.getenv("AGENT_TIMEOUT", "180")),
        max_files=int(os.getenv("MAX_FILES", "250")),
        max_file_chars=int(os.getenv("MAX_FILE_CHARS", "12000")),
        max_total_chars=int(os.getenv("MAX_TOTAL_CHARS", "48000")),
        prompts_dir=root / "agents",
        output_dir=root / "output",
        reviewer_model=reviewer_model,
        interactive=False,
    )


