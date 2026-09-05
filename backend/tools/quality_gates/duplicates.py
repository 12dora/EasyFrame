"""Cross-file duplicate detector using normalised 40-line windows."""

from __future__ import annotations

import hashlib
import io
import keyword
import tokenize
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from tools.quality_gates.code_size import ScanSpec, python_files, relative_to_repo

WINDOW_SIZE = 40
_SKIPPABLE_KINDS = frozenset({"import", "decorator", "field"})
_IGNORE_TOKEN_TYPES = frozenset(
    {
        tokenize.NEWLINE,
        tokenize.NL,
        tokenize.ENCODING,
        tokenize.ENDMARKER,
        tokenize.COMMENT,
        tokenize.INDENT,
        tokenize.DEDENT,
    }
)


@dataclass(frozen=True)
class DuplicateViolation:
    path: str
    start_line: int
    end_line: int
    value: int
    peers: tuple[str, ...]

    def format(self) -> str:
        peer = ", ".join(self.peers) if self.peers else "?"
        return f"{self.path}:{self.start_line}-{self.end_line}: {self.value} normalised lines duplicated with {peer}"


def scan_duplicate_windows(
    repo_root: Path | str,
    spec: ScanSpec,
    *,
    window_size: int = WINDOW_SIZE,
) -> list[DuplicateViolation]:
    """Find cross-file clones of at least ``window_size`` normalised lines."""

    root_path = Path(repo_root).resolve()
    normalised = {path: _normalise_file(path) for path in python_files(root_path, spec)}
    coverage = _pair_coverage(normalised, window_size)
    return _pair_violations(root_path, normalised, coverage)


def _pair_coverage(
    normalised: dict[Path, list[_NormLine]],
    window_size: int,
) -> dict[tuple[Path, Path], set[int]]:
    coverage: dict[tuple[Path, Path], set[int]] = defaultdict(set)
    for locations in _window_index(normalised, window_size).values():
        _add_cross_file_coverage(coverage, locations, window_size)
    return coverage


def _window_index(
    normalised: dict[Path, list[_NormLine]],
    window_size: int,
) -> dict[str, list[tuple[Path, int]]]:
    index: dict[str, list[tuple[Path, int]]] = defaultdict(list)
    for path, lines in normalised.items():
        if len(lines) < window_size:
            continue
        for start in range(0, len(lines) - window_size + 1):
            if _window_skippable(lines, start, window_size):
                continue
            index[_window_digest(lines, start, window_size)].append((path, start))
    return index


def _add_cross_file_coverage(
    coverage: dict[tuple[Path, Path], set[int]],
    locations: list[tuple[Path, int]],
    window_size: int,
) -> None:
    by_path: dict[Path, list[int]] = defaultdict(list)
    for path, start in locations:
        by_path[path].append(start)
    if len(by_path) < 2:
        return
    paths = list(by_path)
    for index_a, path_a in enumerate(paths):
        for path_b in paths[index_a + 1 :]:
            _cover_pair(coverage, path_a, path_b, by_path[path_a], window_size)
            _cover_pair(coverage, path_b, path_a, by_path[path_b], window_size)


def _cover_pair(
    coverage: dict[tuple[Path, Path], set[int]],
    path: Path,
    peer: Path,
    starts: list[int],
    window_size: int,
) -> None:
    covered = coverage[(path, peer)]
    for start in starts:
        covered.update(range(start, start + window_size))


def _pair_violations(
    root_path: Path,
    normalised: dict[Path, list[_NormLine]],
    coverage: dict[tuple[Path, Path], set[int]],
) -> list[DuplicateViolation]:
    violations: list[DuplicateViolation] = []
    for (path, peer), indices in coverage.items():
        if not indices:
            continue
        lines = normalised[path]
        begin = min(indices)
        end = max(indices)
        violations.append(
            DuplicateViolation(
                path=relative_to_repo(root_path, path),
                start_line=lines[begin].lineno,
                end_line=lines[end].lineno,
                value=len(indices),
                peers=(relative_to_repo(root_path, peer),),
            )
        )
    return sorted(violations, key=lambda item: (-item.value, item.path, item.peers, item.start_line))


@dataclass(frozen=True)
class _NormLine:
    lineno: int
    text: str
    skippable: bool


def _normalise_file(path: Path) -> list[_NormLine]:
    result: list[_NormLine] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        tokens = _line_tokens(raw)
        if tokens is None:
            continue
        kind = _line_kind(tokens)
        if kind == "empty":
            continue
        result.append(
            _NormLine(
                lineno=lineno,
                text=_tokens_to_text(tokens),
                skippable=kind in _SKIPPABLE_KINDS,
            )
        )
    return result


def _line_tokens(raw: str) -> list[tokenize.TokenInfo] | None:
    stripped = raw.strip()
    if not stripped or stripped.startswith("#"):
        return None
    try:
        tokens = [
            tok
            for tok in tokenize.generate_tokens(io.StringIO(stripped).readline)
            if tok.type not in _IGNORE_TOKEN_TYPES and tok.string
        ]
    except tokenize.TokenError:
        return _fallback_tokens(stripped)
    return tokens or None


def _fallback_tokens(stripped: str) -> list[tokenize.TokenInfo] | None:
    text = _collapse_ws_outside_strings(stripped)
    if not text:
        return None
    return [tokenize.TokenInfo(tokenize.STRING, text, (1, 0), (1, len(text)), stripped)]


def _collapse_ws_outside_strings(text: str) -> str:
    parts: list[str] = []
    in_string = False
    quote = ""
    escaped = False
    buf: list[str] = []

    def flush_code() -> None:
        chunk = "".join(buf).strip()
        buf.clear()
        if chunk:
            parts.append(" ".join(chunk.split()))

    for char in text:
        if in_string:
            buf.append(char)
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == quote:
                parts.append("".join(buf))
                buf.clear()
                in_string = False
                quote = ""
            continue
        if char in {'"', "'"}:
            flush_code()
            in_string = True
            quote = char
            buf.append(char)
            continue
        buf.append(char)
    if in_string:
        parts.append("".join(buf))
    else:
        flush_code()
    return " ".join(part for part in parts if part)


def _tokens_to_text(tokens: list[tokenize.TokenInfo]) -> str:
    return " ".join(tok.string for tok in tokens)


def _line_kind(tokens: list[tokenize.TokenInfo]) -> str:
    if not tokens:
        return "empty"
    first = tokens[0].string
    if first in {"import", "from"}:
        return "import"
    if first == "@":
        return "decorator"
    if tokens[0].type == tokenize.NAME and first not in keyword.kwlist and len(tokens) >= 2 and tokens[1].string == ":":
        return "field"
    return "code"


def _window_digest(lines: list[_NormLine], start: int, window_size: int) -> str:
    payload = "\n".join(item.text for item in lines[start : start + window_size]).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _window_skippable(lines: list[_NormLine], start: int, window_size: int) -> bool:
    return all(lines[start + offset].skippable for offset in range(window_size))
