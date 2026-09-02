from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


def normalize_rel_path(path_str: str) -> str:
    """
    Normalizes relative file paths:
    - Strips whitespace, quotes, and backticks
    - Converts backslashes to forward slashes
    - Removes leading './' or '/'
    - Resolves redundant slashes
    """
    if not path_str or not isinstance(path_str, str):
        return ""
    p = path_str.strip().strip("'\"`")
    p = p.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    p = re.sub(r"/+", "/", p)
    return p.strip()


IGNORE_DIRS = {
    ".git", ".idea", ".vscode", ".venv", "venv", "__pycache__",
    "node_modules", "dist", "build", ".next", ".nuxt", "coverage",
    "vendor", "Pods", "DerivedData", ".turbo", ".cache",
}

TEXT_SUFFIXES = {
    ".md", ".txt", ".py", ".js", ".jsx", ".ts", ".tsx", ".json",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env.example",
    ".sql", ".graphql", ".gql", ".css", ".scss", ".html", ".sh",
    ".dockerfile", ".go", ".rs", ".java", ".kt", ".swift",
}

IMPORTANT_NAMES = {
    "README", "README.md", "package.json", "pyproject.toml",
    "requirements.txt", "Dockerfile", "docker-compose.yml",
    "docker-compose.yaml", "compose.yml", "compose.yaml",
    "Makefile", "AGENTS.md", "CLAUDE.md", ".env.example",
    ".env.sample", ".env.template", "pnpm-workspace.yaml",
    "turbo.json", "nx.json", "biome.json", "drizzle.config.ts",
    "drizzle.config.js", "schema.prisma", "tsconfig.json",
    "vite.config.ts", "vite.config.js", "next.config.js",
    "next.config.mjs", "next.config.ts",
}

LOCKFILE_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "pnpm-lock.yml",
    "poetry.lock", "Cargo.lock", "Gemfile.lock", "composer.lock",
}

ROLE_CHAR_LIMITS: dict[str, int] = {
    "profiler": 30_000,
    "planner": 35_000,
    "builder": 45_000,
    "reviewer": 35_000,
}

ROLE_FILE_LIMITS: dict[str, int] = {
    "profiler": 25,
    "planner": 35,
    "builder": 15,
    "reviewer": 25,
}


@dataclass
class RepoSnapshot:
    root: Path
    tree: str
    content: str
    files_included: list[str]
    total_chars: int
    candidates_total: int = 0
    candidates_discarded: int = 0


def _is_candidate(path: Path) -> bool:
    if path.name in LOCKFILE_NAMES:
        return False
    if path.name in IMPORTANT_NAMES:
        return True
    if path.name.startswith(".env") and path.name != ".env":
        return True
    return path.suffix.lower() in TEXT_SUFFIXES


def build_global_inventory_tree(root: Path, max_depth: int = 4) -> str:
    """Builds a bounded inventory tree of the repository."""
    lines: list[str] = []

    def _walk(current: Path, depth: int, prefix: str = ""):
        if depth > max_depth or len(lines) >= 350:
            return
        try:
            entries = sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except (PermissionError, OSError):
            return

        for entry in entries:
            if entry.name in IGNORE_DIRS or entry.name.startswith(".git"):
                continue
            rel = entry.relative_to(root)
            rel_str = str(rel).replace("\\", "/")
            if entry.is_dir():
                lines.append(f"{prefix}{entry.name}/")
                _walk(entry, depth + 1, prefix + "  ")
            else:
                lines.append(f"{prefix}{entry.name}")

    _walk(root, 1)
    return "\n".join(lines[:350])


def build_profiler_snapshot(
    root: Path,
    max_file_chars: int = 12_000,
    max_total_chars: int = 30_000,
) -> RepoSnapshot:
    """
    Selects root manifests, configurations, env examples, and READMEs
    for the Profiler to understand stack and conventions.
    """
    tree = build_global_inventory_tree(root)
    files_included: list[str] = []
    content_blocks: list[str] = []
    total_chars = 0
    candidates_total = 0
    candidates_discarded = 0

    candidates: list[Path] = []
    for p in root.rglob("*"):
        if any(ignored in p.parts for ignored in IGNORE_DIRS):
            continue
        if p.is_file() and _is_candidate(p):
            candidates.append(p)

    candidates_total = len(candidates)

    def _priority(p: Path) -> tuple:
        rel = p.relative_to(root)
        name = p.name.lower()
        is_root = len(rel.parts) == 1
        is_manifest = name in {"package.json", "pyproject.toml", "requirements.txt", "cargo.toml"}
        is_readme = "readme" in name
        is_config = name in IMPORTANT_NAMES or name.endswith(".json") or name.endswith(".config.ts")
        return (not is_root, not is_manifest, not is_readme, not is_config, len(str(rel)))

    sorted_candidates = sorted(candidates, key=_priority)

    for p in sorted_candidates:
        rel_str = normalize_rel_path(str(p.relative_to(root)))
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        if len(text) > max_file_chars:
            text = text[:max_file_chars] + "\n\n[... CONTENIDO TRUNCADO POR LÍMITE DE TAMAÑO ...]"

        block = f"--- ARCHIVO: {rel_str} ---\n{text}\n"
        if total_chars + len(block) > max_total_chars and files_included:
            candidates_discarded += 1
            continue

        files_included.append(rel_str)
        content_blocks.append(block)
        total_chars += len(block)
        if len(files_included) >= ROLE_FILE_LIMITS.get("profiler", 25):
            break

    return RepoSnapshot(
        root=root,
        tree=tree,
        content="\n".join(content_blocks),
        files_included=files_included,
        total_chars=total_chars,
        candidates_total=candidates_total,
        candidates_discarded=candidates_total - len(files_included),
    )


def build_planner_snapshot(
    root: Path,
    max_file_chars: int = 12_000,
    max_total_chars: int = 35_000,
) -> RepoSnapshot:
    """Builds snapshot for the Planner with tree and architecture files."""
    return build_profiler_snapshot(root, max_file_chars=max_file_chars, max_total_chars=max_total_chars)


def build_builder_snapshot(
    root: Path,
    target_files: list[str],
    full_tree: str = "",
    max_file_chars: int = 25_000,
    max_total_chars: int = 45_000,
) -> RepoSnapshot:
    """
    Builds a targeted snapshot containing the EXACT contents of the files to be patched.
    """
    if not full_tree:
        full_tree = build_global_inventory_tree(root)

    files_included: list[str] = []
    content_blocks: list[str] = []
    total_chars = 0
    discarded = 0

    norm_targets = [normalize_rel_path(p) for p in target_files if p]

    for rel_str in norm_targets:
        if not rel_str:
            continue
        file_path = root / rel_str
        if not file_path.exists() or not file_path.is_file():
            # Could be a file to create
            continue

        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        if len(text) > max_file_chars:
            text = text[:max_file_chars] + "\n\n[... CONTENIDO TRUNCADO ...]"

        block = f"--- ARCHIVO FUENTE ACTUAL: {rel_str} ---\n{text}\n"
        if total_chars + len(block) > max_total_chars and files_included:
            discarded += 1
            continue

        files_included.append(rel_str)
        content_blocks.append(block)
        total_chars += len(block)

    return RepoSnapshot(
        root=root,
        tree=full_tree,
        content="\n".join(content_blocks) if content_blocks else "No se especificaron o no existen archivos fuente previos.",
        files_included=files_included,
        total_chars=total_chars,
        candidates_total=len(norm_targets),
        candidates_discarded=discarded,
    )
