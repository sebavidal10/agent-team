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
    max_files: int
    max_file_chars: int
    max_total_chars: int
    prompts_dir: Path
    output_dir: Path


def load_settings() -> Settings:
    load_dotenv()

    root = Path(__file__).resolve().parents[2]

    return Settings(
        model=os.getenv("AGENT_MODEL", "qwen2.5-coder:7b"),
        base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        num_ctx=int(os.getenv("OLLAMA_NUM_CTX", "16384")),
        max_files=int(os.getenv("MAX_FILES", "250")),
        max_file_chars=int(os.getenv("MAX_FILE_CHARS", "12000")),
        max_total_chars=int(os.getenv("MAX_TOTAL_CHARS", "120000")),
        prompts_dir=root / "agents",
        output_dir=root / "output",
    )
