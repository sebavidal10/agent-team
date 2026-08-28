from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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
    ".env.sample", ".env.template",
}

ROLE_CHAR_LIMITS: dict[str, int] = {
    "architect": 30_000,
    "backend": 35_000,
    "frontend": 35_000,
    "testing": 30_000,
    "docs": 25_000,
    "reviewer": 25_000,
}

ROLE_FILE_LIMITS: dict[str, int] = {
    "architect": 40,
    "backend": 50,
    "frontend": 50,
    "testing": 40,
    "docs": 30,
    "reviewer": 40,
}

ROLE_KEYWORDS = {
    "architect": {
        "dirs": {"docs", ".agent", ".skill"},
        "names": IMPORTANT_NAMES,
        "contains": {
            "architecture", "spec", "decision", "adr", "config",
            "route", "schema", "model",
        },
    },
    "backend": {
        "dirs": {
            "backend", "server", "api", "routes", "controllers",
            "services", "models", "db", "database", "migrations",
            "repositories",
        },
        "names": {
            "package.json", "pyproject.toml", "requirements.txt",
            "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
            "compose.yml", "compose.yaml",
        },
        "contains": {
            "api", "route", "controller", "service", "repository",
            "migration", "schema", "model", "database", "auth",
        },
    },
    "frontend": {
        "dirs": {
            "frontend", "web", "client", "ui", "components",
            "pages", "views", "hooks", "store", "stores",
        },
        "names": {
            "package.json", "vite.config.ts", "vite.config.js",
            "tsconfig.json",
        },
        "contains": {
            "component", "page", "view", "hook", "store",
            "frontend", "client", "ui",
        },
    },
    "testing": {
        "dirs": {
            "tests", "test", "__tests__", "e2e",
            "integration", "unit", "spec",
        },
        "names": {
            "vitest.config.ts", "vitest.config.js",
            "jest.config.ts", "jest.config.js",
            "playwright.config.ts", "playwright.config.js",
            "package.json",
        },
        "contains": {
            "test", "spec", "e2e", "integration",
            "fixture", "mock",
        },
    },
    "docs": {
        "dirs": {
            "docs", ".agent", ".skill",
        },
        "names": IMPORTANT_NAMES,
        "contains": {
            "readme", "spec", "architecture", "decision",
            "adr", "documentation", "guide",
        },
    },
}


LOCKFILE_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "pnpm-lock.yml",
    "poetry.lock", "Cargo.lock", "Gemfile.lock", "composer.lock",
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


def _matches_role(path: Path, root: Path, role: str) -> bool:
    rules = ROLE_KEYWORDS.get(role)

    if not rules:
        return True

    rel = path.relative_to(root)
    parts_lower = {part.lower() for part in rel.parts}
    name_lower = path.name.lower()
    path_lower = str(rel).lower()

    if role == "testing":
        # Testing needs both test files and key production implementation files
        if parts_lower & rules["dirs"] or path.name in rules["names"]:
            return True
        if any(keyword in path_lower or keyword in name_lower for keyword in rules["contains"]):
            return True
        # Also include core source files so testing can audit implementation vs test coverage
        if "src" in parts_lower or "app" in parts_lower or "lib" in parts_lower:
            return path.suffix.lower() in {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs"}
        return False

    if path.name in rules["names"]:
        return True

    if parts_lower & rules["dirs"]:
        return True

    return any(keyword in path_lower or keyword in name_lower
               for keyword in rules["contains"])


def _priority(path: Path, root: Path, role: str) -> tuple:
    rel = path.relative_to(root)
    parts_lower = {part.lower() for part in rel.parts}
    name_lower = path.name.lower()
    path_lower = str(rel).lower()

    is_test_file = (
        any(t in name_lower for t in [".test.", ".spec.", "_test.", "_spec."])
        or bool(parts_lower & {"tests", "test", "__tests__", "e2e", "integration", "unit", "spec"})
    )

    score = 100

    if path.name in IMPORTANT_NAMES:
        score -= 30

    if role == "backend":
        if is_test_file:
            score += 25  # Prioritize production implementation before tests
        else:
            if any(k in path_lower for k in ["route", "controller", "api", "endpoint"]):
                score -= 60
            elif any(k in path_lower for k in ["service", "model", "repository", "schema", "db", "database"]):
                score -= 50
            elif "migration" in path_lower:
                score -= 20
            elif any(k in path_lower for k in ["auth", "middleware", "config"]):
                score -= 30

    elif role == "frontend":
        if is_test_file:
            score += 25  # Prioritize UI components and pages before tests
        else:
            if any(k in path_lower for k in ["page", "view", "component", "ui"]):
                score -= 60
            elif any(k in path_lower for k in ["hook", "store", "state", "context", "client", "router", "app."]):
                score -= 50
            elif any(k in path_lower for k in ["config", "util", "helper"]):
                score -= 20

    elif role == "testing":
        if any(k in name_lower for k in ["vitest", "jest", "playwright", "cypress"]):
            score -= 60
        elif is_test_file:
            score -= 50  # Existing tests
        else:
            # Production files included to compare coverage
            score -= 35

    elif role == "architect":
        if any(k in path_lower for k in ["doc", "adr", "spec", "architecture"]):
            score -= 60
        elif any(k in path_lower for k in ["config", "schema", "main.", "index.", "app."]):
            score -= 40

    elif role == "docs":
        if any(k in path_lower for k in [".env.example", ".env.sample", ".env.template"]):
            score -= 75
        elif any(k in path_lower for k in ["readme", "doc", "spec", "guide", "adr"]):
            score -= 60
        elif any(k in path_lower for k in ["package.json", "pyproject.toml", "docker-compose", "dockerfile"]):
            score -= 40

    if rel.parts and rel.parts[0] in {"src", "app", "lib"}:
        score -= 10

    return (score, len(rel.parts), str(rel))


def build_snapshot(
    root: Path,
    max_files: int | None = None,
    max_file_chars: int = 12000,
    max_total_chars: int | None = None,
    role: str | None = None,
) -> RepoSnapshot:
    root = root.expanduser().resolve()

    if not root.is_dir():
        raise ValueError(f"No existe el directorio: {root}")

    role_key = role or "architect"
    effective_max_chars = max_total_chars or ROLE_CHAR_LIMITS.get(role_key, 60_000)
    effective_max_files = max_files or ROLE_FILE_LIMITS.get(role_key, 50)

    candidates: list[Path] = []

    for path in root.rglob("*"):
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue

        if any(part in IGNORE_DIRS for part in rel.parts):
            continue

        if not path.is_file():
            continue

        if not _is_candidate(path):
            continue

        if role and not _matches_role(path, root, role):
            continue

        candidates.append(path)

    total_candidates = len(candidates)

    candidates.sort(
        key=lambda p: _priority(
            p,
            root=root,
            role=role_key,
        )
    )

    # Lightweight inventory tree includes all matched candidate paths
    inventory_tree_lines = [str(p.relative_to(root)) for p in candidates]

    candidates = candidates[:effective_max_files]

    included: list[str] = []
    chunks: list[str] = []
    total = 0

    for path in candidates:
        rel = path.relative_to(root)

        try:
            text = path.read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            continue

        if len(text) > max_file_chars:
            text = (
                text[:max_file_chars]
                + "\n...[TRUNCADO]..."
            )

        block = (
            f"\n\n===== FILE: {rel} =====\n"
            f"{text}"
        )

        if total + len(block) > effective_max_chars:
            # Candidate does not fit in remaining budget; continue evaluating remaining smaller candidates
            continue

        chunks.append(block)
        included.append(str(rel))
        total += len(block)

    discarded = total_candidates - len(included)

    return RepoSnapshot(
        root=root,
        tree="\n".join(inventory_tree_lines),
        content="".join(chunks),
        files_included=included,
        total_chars=total,
        candidates_total=total_candidates,
        candidates_discarded=max(0, discarded),
    )


def build_role_snapshots(
    root: Path,
    max_files: int | None = None,
    max_file_chars: int = 12000,
    max_total_chars: int | None = None,
    role_char_limits: dict[str, int] | None = None,
) -> dict[str, RepoSnapshot]:
    roles = [
        "architect",
        "backend",
        "frontend",
        "testing",
        "docs",
    ]

    char_limits = role_char_limits or ROLE_CHAR_LIMITS

    return {
        role: build_snapshot(
            root=root,
            max_files=max_files,
            max_file_chars=max_file_chars,
            max_total_chars=char_limits.get(role, max_total_chars),
            role=role,
        )
        for role in roles
    }


def build_global_inventory_tree(root: Path) -> str:
    """Builds a complete lightweight inventory tree of all code/text files in the repository."""
    root = root.expanduser().resolve()
    paths: list[str] = []
    for path in root.rglob("*"):
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if any(part in IGNORE_DIRS for part in rel.parts):
            continue
        if not path.is_file():
            continue
        if not _is_candidate(path):
            continue
        paths.append(str(rel))
    paths.sort()
    return "\n".join(paths)


def build_targeted_snapshot(
    root: Path,
    target_files: set[str] | list[str],
    full_inventory_tree: str,
    max_file_chars: int = 12000,
    max_total_chars: int = 25000,
) -> RepoSnapshot:
    """
    Builds an evidence-targeted snapshot for Reviewer containing only files cited by specialists,
    preventing bloated 50k+ char context and token bottlenecks.
    """
    root = root.expanduser().resolve()
    included: list[str] = []
    chunks: list[str] = []
    total = 0

    valid_targets = [
        f for f in sorted(target_files)
        if f and f.lower() not in {"n/a", "none", "unknown", "null"}
    ]

    for rel_path_str in valid_targets:
        path = root / rel_path_str
        if not path.is_file():
            continue

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        if len(text) > max_file_chars:
            text = text[:max_file_chars] + "\n...[TRUNCADO]..."

        block = f"\n\n===== FILE: {rel_path_str} =====\n{text}"
        if total + len(block) > max_total_chars:
            continue

        chunks.append(block)
        included.append(rel_path_str)
        total += len(block)

    discarded = len(valid_targets) - len(included)

    return RepoSnapshot(
        root=root,
        tree=full_inventory_tree,
        content="".join(chunks),
        files_included=included,
        total_chars=total,
        candidates_total=len(valid_targets),
        candidates_discarded=max(0, discarded),
    )

