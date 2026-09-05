"""Code-size scanners for backend quality gates."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

PYTHON_FILE_KIND = "python-file"
TEXT_FILE_KIND = "text-file"
PYTHON_FUNCTION_KIND = "python-function"
MODULE_QUALIFIED_NAME = "<module>"

SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".ruff_cache",
        ".pytest_cache",
        "node_modules",
        "easyframe_platform.egg-info",
    }
)


@dataclass(frozen=True)
class CodeSizeThresholds:
    """SLOC 阈值表。下游可通过 gates.json 覆盖,默认与 EasyTrade 口径一致。"""

    production_file: int = 500
    route_file: int = 180
    test_file: int = 400
    fixture_file: int = 400
    function: int = 40
    migration: int = 60
    test_function: int = 80


@dataclass(frozen=True)
class ScanSpec:
    """扫描根目录与路径分类规则(相对 repo 根的 posix 路径)。"""

    roots: tuple[str, ...]
    test_substrings: tuple[str, ...] = ("/tests/", "/platform_tests/")
    route_substrings: tuple[str, ...] = ("/api/",)
    route_exclude_substrings: tuple[str, ...] = ("/schemas/", "/dependencies/")
    migration_substrings: tuple[str, ...] = ("/alembic/versions/",)
    fixture_globs: tuple[str, ...] = ("*.json",)


@dataclass(frozen=True)
class CodeSizeViolation:
    path: str
    kind: str
    qualifiedName: str  # noqa: N815 - scanner contract uses camelCase report fields
    startLine: int  # noqa: N815 - scanner contract uses camelCase report fields
    endLine: int  # noqa: N815 - scanner contract uses camelCase report fields
    sloc: int
    threshold: int
    reason: str = "new oversized item"

    def format(self) -> str:
        location = f"{self.path}:{self.startLine}-{self.endLine}"
        return (
            f"{location}: {self.kind} {self.qualifiedName} has {self.sloc} SLOC "
            f"(threshold {self.threshold}): {self.reason}"
        )


def scan_python_file_size_violations(
    repo_root: Path | str,
    spec: ScanSpec,
    thresholds: CodeSizeThresholds | None = None,
) -> list[CodeSizeViolation]:
    """扫描 Python 文件 SLOC 是否超过分类阈值。"""

    root_path = Path(repo_root).resolve()
    limits = thresholds or CodeSizeThresholds()
    violations: list[CodeSizeViolation] = []
    for path in _python_paths(root_path, spec):
        rel_path = _relative_path(root_path, path)
        lines = path.read_text(encoding="utf-8").splitlines()
        threshold = _python_file_sloc_threshold(rel_path, spec, limits)
        sloc = _count_non_comment_sloc(lines)
        if sloc <= threshold:
            continue
        violations.append(
            CodeSizeViolation(
                path=rel_path,
                kind=PYTHON_FILE_KIND,
                qualifiedName=MODULE_QUALIFIED_NAME,
                startLine=1,
                endLine=len(lines),
                sloc=sloc,
                threshold=threshold,
            )
        )
    return _sort_code_size_violations(violations)


def scan_text_file_size_violations(
    repo_root: Path | str,
    spec: ScanSpec,
    thresholds: CodeSizeThresholds | None = None,
) -> list[CodeSizeViolation]:
    """扫描测试 fixture 文本文件 SLOC 是否超过阈值。"""

    root_path = Path(repo_root).resolve()
    limits = thresholds or CodeSizeThresholds()
    violations: list[CodeSizeViolation] = []
    for path in _text_paths(root_path, spec):
        rel_path = _relative_path(root_path, path)
        lines = path.read_text(encoding="utf-8").splitlines()
        sloc = _count_text_sloc(lines)
        if sloc <= limits.fixture_file:
            continue
        violations.append(
            CodeSizeViolation(
                path=rel_path,
                kind=TEXT_FILE_KIND,
                qualifiedName=MODULE_QUALIFIED_NAME,
                startLine=1,
                endLine=len(lines),
                sloc=sloc,
                threshold=limits.fixture_file,
            )
        )
    return _sort_code_size_violations(violations)


def scan_python_function_size_violations(
    repo_root: Path | str,
    spec: ScanSpec,
    thresholds: CodeSizeThresholds | None = None,
) -> list[CodeSizeViolation]:
    """扫描 Python function / async function / method SLOC。"""

    root_path = Path(repo_root).resolve()
    limits = thresholds or CodeSizeThresholds()
    violations: list[CodeSizeViolation] = []
    for path in _python_paths(root_path, spec):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        rel_path = _relative_path(root_path, path)
        scanner = _CodeSizeFunctionScanner(
            path=rel_path,
            lines=source.splitlines(),
            threshold=_python_function_sloc_threshold(rel_path, spec, limits),
        )
        scanner.visit(tree)
        violations.extend(scanner.violations)
    return _sort_code_size_violations(violations)


def python_files(repo_root: Path | str, spec: ScanSpec) -> list[Path]:
    """Scan roots 下的生产/测试 Python 文件(跳过 venv 等)。"""

    return _python_paths(Path(repo_root).resolve(), spec)


def relative_to_repo(repo_root: Path | str, path: Path) -> str:
    return _relative_path(Path(repo_root).resolve(), path)


def scan_code_size_violations(
    repo_root: Path | str,
    spec: ScanSpec,
    thresholds: CodeSizeThresholds | None = None,
) -> list[CodeSizeViolation]:
    """扫描 Python 文件、fixture 文本和 Python symbol 体积。"""

    limits = thresholds or CodeSizeThresholds()
    return _sort_code_size_violations(
        [
            *scan_python_file_size_violations(repo_root, spec, limits),
            *scan_text_file_size_violations(repo_root, spec, limits),
            *scan_python_function_size_violations(repo_root, spec, limits),
        ]
    )


class _CodeSizeFunctionScanner(ast.NodeVisitor):
    def __init__(self, *, path: str, lines: list[str], threshold: int) -> None:
        self.path = path
        self.lines = lines
        self.threshold = threshold
        self.scope_stack: list[str] = []
        self.violations: list[CodeSizeViolation] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        qualified_name = ".".join([*self.scope_stack, node.name])
        sloc = _function_sloc(self.lines, node)
        if sloc > self.threshold:
            self.violations.append(
                CodeSizeViolation(
                    path=self.path,
                    kind=PYTHON_FUNCTION_KIND,
                    qualifiedName=qualified_name,
                    startLine=_function_start_line(node),
                    endLine=node.end_lineno or node.lineno,
                    sloc=sloc,
                    threshold=self.threshold,
                )
            )
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()


def _python_paths(repo_root: Path, spec: ScanSpec) -> list[Path]:
    found: set[Path] = set()
    for root in _existing_roots(repo_root, spec.roots):
        for path in root.rglob("*.py"):
            if path.is_file() and not _is_skipped(path, repo_root):
                found.add(path.resolve())
    return sorted(found)


def _text_paths(repo_root: Path, spec: ScanSpec) -> list[Path]:
    found: set[Path] = set()
    for root in _existing_roots(repo_root, spec.roots):
        for pattern in spec.fixture_globs:
            for path in root.rglob(pattern):
                if not (path.is_file() and path.suffix == ".json") or _is_skipped(path, repo_root):
                    continue
                if _is_test_path(_relative_path(repo_root, path), spec):
                    found.add(path.resolve())
    return sorted(found)


def _existing_roots(repo_root: Path, roots: tuple[str, ...]) -> list[Path]:
    result: list[Path] = []
    for raw in roots:
        path = (repo_root / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
        if path.is_dir():
            result.append(path)
    return result


def _is_skipped(path: Path, repo_root: Path) -> bool:
    try:
        parts = path.resolve().relative_to(repo_root).parts
    except ValueError:
        parts = path.parts
    return any(part in SKIP_DIR_NAMES for part in parts)


def _relative_path(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root).as_posix()


def _python_file_sloc_threshold(rel_path: str, spec: ScanSpec, thresholds: CodeSizeThresholds) -> int:
    if _is_test_path(rel_path, spec):
        return thresholds.test_file
    if _is_route_path(rel_path, spec):
        return thresholds.route_file
    return thresholds.production_file


def _python_function_sloc_threshold(rel_path: str, spec: ScanSpec, thresholds: CodeSizeThresholds) -> int:
    if _is_test_path(rel_path, spec):
        return thresholds.test_function
    if _is_migration_path(rel_path, spec):
        return thresholds.migration
    return thresholds.function


def _posix_rel(rel_path: str) -> str:
    return rel_path.replace("\\", "/")


def _is_test_path(rel_path: str, spec: ScanSpec | None = None) -> bool:
    padded = f"/{_posix_rel(rel_path).strip('/')}/"
    needles = spec.test_substrings if spec is not None else ("/tests/", "/platform_tests/")
    return any(token in padded for token in needles)


def _is_route_path(rel_path: str, spec: ScanSpec) -> bool:
    padded = f"/{_posix_rel(rel_path)}"
    if any(token in padded for token in spec.route_exclude_substrings):
        return False
    return any(token in padded for token in spec.route_substrings)


def _is_migration_path(rel_path: str, spec: ScanSpec) -> bool:
    padded = f"/{_posix_rel(rel_path)}"
    return any(token in padded for token in spec.migration_substrings)


def _count_non_comment_sloc(lines: list[str]) -> int:
    return sum(1 for line in lines if _is_sloc_line(line))


def _count_text_sloc(lines: list[str]) -> int:
    return sum(1 for line in lines if line.strip())


def _function_sloc(lines: list[str], node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    end_lineno = node.end_lineno or node.lineno
    docstring_lines = _docstring_lines(node)
    return sum(
        1
        for line_no in range(_function_start_line(node), end_lineno + 1)
        if line_no not in docstring_lines and _is_sloc_line(lines[line_no - 1])
    )


def _docstring_lines(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[int]:
    if not node.body:
        return set()
    first_stmt = node.body[0]
    if not (
        isinstance(first_stmt, ast.Expr)
        and isinstance(first_stmt.value, ast.Constant)
        and isinstance(first_stmt.value.value, str)
    ):
        return set()
    end_lineno = first_stmt.end_lineno or first_stmt.lineno
    return set(range(first_stmt.lineno, end_lineno + 1))


def _function_start_line(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    return min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])


def _is_sloc_line(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _sort_code_size_violations(violations: list[CodeSizeViolation]) -> list[CodeSizeViolation]:
    return sorted(
        violations,
        key=lambda item: (-item.sloc, item.path, item.kind, item.qualifiedName),
    )
