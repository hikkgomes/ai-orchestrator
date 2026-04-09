"""Regex-based heuristic scanner for common AI-generated code gotchas."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import fnmatch
from pathlib import Path
import re


DEFAULT_IGNORE_PARTS = {
    ".git",
    ".next",
    ".turbo",
    ".cache",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "coverage",
    "target",
    "__pycache__",
}
DEFAULT_IGNORE_PREFIXES = [
    ".git/",
    "node_modules/",
    ".next/",
    "dist/",
    "build/",
    "coverage/",
    ".turbo/",
    ".vercel/",
    ".cache/",
    "vendor/",
    "target/",
]
LOCKFILES = {
    "Cargo.lock",
    "Gemfile.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    "yarn.lock",
}
TEXT_EXTENSIONS = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".java",
    ".kt",
    ".kts",
    ".cs",
    ".swift",
    ".sh",
    ".bash",
    ".zsh",
    ".ps1",
    ".sql",
    ".tf",
    ".yml",
    ".yaml",
    ".json",
    ".toml",
    ".ini",
    ".cfg",
    ".md",
    ".txt",
}
SKIP_PATTERNS = {
    "hardcoded_ip": ["*.json", "*.yaml", "*.yml", "*.toml", "Dockerfile", "*.cfg"],
    "hardcoded_credential": ["*.example", "*.sample", "*.md"],
    "debug_print": ["*test*", "*spec*", "**/scripts/**", "*.md"],
    "unhandled_promise": ["*test*"],
    "env_no_default": ["*.d.ts", "*.md"],
    "hardcoded_url": ["*.md", "*.txt", "*.lock"],
    "magic_number": ["*.json", "*.yaml", "*.yml", "*.css", "*test*", "*.lock"],
}
PATTERNS = [
    (
        "placeholder",
        re.compile(r"\b(TODO|FIXME|changeme|dummy[_ -]?key|your[_ -]?api[_ -]?key|lorem ipsum)\b", re.I),
    ),
    ("swallowed_exception", re.compile(r"except\s*:\s*pass|catch\s*\([^)]*\)\s*\{\s*\}", re.I | re.S)),
    ("sql_concatenation", re.compile(r"(SELECT|INSERT|UPDATE|DELETE).*(\+|%s|f['\"]).*", re.I | re.S)),
    ("tls_disabled", re.compile(r"verify\s*=\s*False|rejectUnauthorized\s*:\s*false|NODE_TLS_REJECT_UNAUTHORIZED", re.I)),
    (
        "secret_logging",
        re.compile(r"console\.log\(.*(token|secret|password|api[_-]?key)|logger\..*\b(token|secret|password|api[_-]?key)\b", re.I),
    ),
    ("shell_injection_risk", re.compile(r"(subprocess\.(run|Popen)\(|exec\(|spawn\(|os\.system\().*(\+|f['\"]|\$\{)", re.I | re.S)),
    ("hardcoded_ip", re.compile(r"['\"](?:\d{1,3}\.){3}\d{1,3}['\"]")),
    ("hardcoded_credential", re.compile(r"\b(password|secret|api[_-]?key|access[_-]?key|token)\b\s*[:=]\s*['\"][^'\"]+['\"]", re.I)),
    ("debug_print", re.compile(r"console\.log\s*\(|\bprint\s*\(|fmt\.Print(?:f|ln)?\s*\(|System\.out\.print(?:ln)?\s*\(|\bdebugger\b")),
    ("dead_code_marker", re.compile(r"(//|#|/\*+)\s*(HACK|XXX|TEMP|REMOVEME)\b", re.I)),
    ("unsafe_eval", re.compile(r"\beval\s*\(|new Function\s*\(|(?<!\.)\bexec\s*\(", re.I)),
    ("unhandled_promise", re.compile(r"\.then\s*\((?:(?!\.catch|\.finally).)*\)(?!\s*\.(catch|finally))", re.I | re.S)),
    ("env_no_default", re.compile(r"process\.env(?:\.[A-Z0-9_]+|\[['\"][A-Z0-9_]+['\"]\])(?!\s*(\|\||\?\?))|os\.environ\[['\"][A-Z0-9_]+['\"]\]", re.I)),
    ("unsafe_deserialization", re.compile(r"pickle\.load\s*\(|yaml\.load\s*\((?![^)]*Loader\s*=)|Marshal\.load\s*\(|\bunserialize\s*\(", re.I | re.S)),
    ("broad_exception", re.compile(r"catch\s*\(\s*(Exception|Error)\b|except\s+Exception\b|^\s*rescue\s*$", re.I | re.M)),
    ("hardcoded_url", re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|staging[\w.-]*|dev[\w.-]*|internal[\w.-]*|corp[\w.-]*|local[\w.-]*)", re.I)),
    ("magic_number", re.compile(r"(?<![\w.])(1\d{3,}|[2-9]\d{3,})(?![\w.])")),
    ("empty_catch", re.compile(r"catch\s*(?:\([^)]*\))?\s*\{\s*\}|except(?:\s+[A-Za-z0-9_., ()]+)?\s*:\s*pass", re.I | re.S)),
]


@dataclass(frozen=True)
class HeuristicFinding:
    workspace: str
    rule_id: str
    line: int
    file: str
    snippet: str


def normalize_rel(path: str) -> str:
    return Path(path).as_posix().lstrip("./")


def is_binary(path: Path) -> bool:
    try:
        return b"\x00" in path.read_bytes()
    except OSError:
        return True


def matches_any(rel: str, patterns: list[str]) -> bool:
    rel_posix = normalize_rel(rel)
    filename = Path(rel_posix).name
    return any(fnmatch.fnmatch(rel_posix, pattern) or fnmatch.fnmatch(filename, pattern) for pattern in patterns)


def workspace_entries(config: dict | None) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for name, payload in ((config or {}).get("workspaces") or {}).items():
        rel_root = normalize_rel((payload or {}).get("root") or name)
        entries.append((name, rel_root))
    entries.sort(key=lambda item: len(item[1]), reverse=True)
    return entries


def ignore_prefixes(config: dict | None) -> list[str]:
    prefixes = [normalize_rel(path) for path in DEFAULT_IGNORE_PREFIXES]
    for key in ("ignore", "generated"):
        for path in (((config or {}).get("paths") or {}).get(key) or []):
            prefixes.append(normalize_rel(path))
    return list(dict.fromkeys(prefixes))


def should_ignore(rel: str, *, config: dict | None) -> bool:
    rel_posix = normalize_rel(rel)
    parts = Path(rel_posix).parts
    if any(part in DEFAULT_IGNORE_PARTS for part in parts):
        return True
    if Path(rel_posix).name in LOCKFILES:
        return True
    for prefix in ignore_prefixes(config):
        cleaned = prefix.rstrip("/")
        if cleaned and (rel_posix == cleaned or rel_posix.startswith(f"{cleaned}/")):
            return True
    return False


def list_candidate_files(root: Path, *, files: list[str] | None, config: dict | None) -> list[str]:
    if files:
        candidates = [normalize_rel(path) for path in files if path.strip()]
        return [path for path in candidates if not should_ignore(path, config=config)]

    discovered: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = normalize_rel(path.relative_to(root).as_posix())
        if should_ignore(rel, config=config):
            continue
        discovered.append(rel)
        if len(discovered) >= 500:
            break
    return discovered


def label_for_workspace(rel: str, config: dict | None) -> str:
    rel_posix = normalize_rel(rel)
    for name, root in workspace_entries(config):
        cleaned = root.rstrip("/")
        if cleaned and (rel_posix == cleaned or rel_posix.startswith(f"{cleaned}/")):
            return name
    return ""


def load_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _add_finding(
    findings: list[HeuristicFinding],
    *,
    config: dict | None,
    rule_id: str,
    rel: str,
    line: int,
    snippet: str,
) -> None:
    findings.append(
        HeuristicFinding(
            workspace=label_for_workspace(rel, config),
            rule_id=rule_id,
            line=line,
            file=rel,
            snippet=snippet.replace("\n", " ")[:140],
        )
    )


def detect_python_long_functions(text: str) -> list[tuple[int, str]]:
    lines = text.splitlines()
    findings: list[tuple[int, str]] = []
    current: dict[str, int | str] | None = None
    for index, line in enumerate(lines, start=1):
        match = re.match(r"^(\s*)(async\s+def|def)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
        if match:
            indent = len(match.group(1))
            if current and index - int(current["start"]) > 80:
                findings.append((int(current["start"]), str(current["name"])))
            current = {"start": index, "indent": indent, "name": match.group(3)}
            continue
        if not current:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= int(current["indent"]) and not stripped.startswith("#"):
            if index - int(current["start"]) > 80:
                findings.append((int(current["start"]), str(current["name"])))
            current = None
    if current and len(lines) + 1 - int(current["start"]) > 80:
        findings.append((int(current["start"]), str(current["name"])))
    return findings


def detect_brace_long_functions(text: str) -> list[tuple[int, str]]:
    lines = text.splitlines()
    findings: list[tuple[int, str]] = []
    start_regex = re.compile(
        r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\("
        r"|^\s*(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>\s*\{"
        r"|^\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*\([^)]*\)\s*\{"
    )
    current: dict[str, int | str] | None = None
    brace_depth = 0
    for index, line in enumerate(lines, start=1):
        if current is None:
            match = start_regex.match(line)
            if not match:
                continue
            name = next(group for group in match.groups() if group)
            current = {"start": index, "name": name}
            brace_depth = line.count("{") - line.count("}")
            if brace_depth <= 0:
                current = None
            continue
        brace_depth += line.count("{") - line.count("}")
        if brace_depth <= 0:
            if index - int(current["start"]) > 80:
                findings.append((int(current["start"]), str(current["name"])))
            current = None
    return findings


def detect_magic_strings(text: str) -> list[tuple[int, str]]:
    pattern = re.compile(r"(['\"])([^'\"\n\\]{10,})\1")
    counts: Counter[str] = Counter()
    first_line: dict[str, int] = {}
    for match in pattern.finditer(text):
        literal = match.group(2).strip()
        if literal.startswith("http://") or literal.startswith("https://"):
            continue
        counts[literal] += 1
        first_line.setdefault(literal, text.count("\n", 0, match.start()) + 1)
    return [(first_line[literal], literal) for literal, count in counts.items() if count >= 4]


def scan_file(root: Path, rel: str, *, config: dict | None) -> list[HeuristicFinding]:
    path = root / rel
    findings: list[HeuristicFinding] = []
    if not path.exists() or not path.is_file():
        return findings
    if should_ignore(rel, config=config):
        return findings
    if path.suffix.lower() not in TEXT_EXTENSIONS and path.name not in {"Dockerfile", "Makefile"}:
        return findings
    if is_binary(path):
        return findings

    text = load_text(path)
    if not text:
        return findings

    for rule_id, regex in PATTERNS:
        if matches_any(rel, SKIP_PATTERNS.get(rule_id, [])):
            continue
        for match in regex.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            _add_finding(findings, config=config, rule_id=rule_id, rel=rel, line=line, snippet=match.group(0))

    todo_matches = list(re.finditer(r"\b(TODO|FIXME)\b", text, re.I))
    if len(todo_matches) > 5:
        _add_finding(
            findings,
            config=config,
            rule_id="high_todo_density",
            rel=rel,
            line=text.count("\n", 0, todo_matches[0].start()) + 1,
            snippet=f"{len(todo_matches)} TODO/FIXME markers",
        )

    if path.suffix.lower() == ".py":
        for line, name in detect_python_long_functions(text):
            _add_finding(findings, config=config, rule_id="long_function", rel=rel, line=line, snippet=f"{name} exceeds 80 lines")
    elif path.suffix.lower() in {".js", ".jsx", ".ts", ".tsx"}:
        for line, name in detect_brace_long_functions(text):
            _add_finding(findings, config=config, rule_id="long_function", rel=rel, line=line, snippet=f"{name} exceeds 80 lines")

    for line, literal in detect_magic_strings(text):
        _add_finding(findings, config=config, rule_id="magic_string", rel=rel, line=line, snippet=literal)

    return findings


def scan_repository(
    root: Path,
    *,
    files: list[str] | None = None,
    config: dict | None = None,
) -> list[HeuristicFinding]:
    """Scan selected files in *root* for heuristic gotchas."""

    findings: list[HeuristicFinding] = []
    for rel in list_candidate_files(root.resolve(), files=files, config=config):
        findings.extend(scan_file(root.resolve(), rel, config=config))
    findings.sort(key=lambda item: (item.file, item.line, item.rule_id, item.workspace))
    return findings
