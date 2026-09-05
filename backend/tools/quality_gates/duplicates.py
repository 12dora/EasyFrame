"""Cross-file duplicate detector using normalised 40-line windows."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from tools.quality_gates.code_size import ScanSpec, python_files, relative_to_repo

WINDOW_SIZE = 40


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
    starts, peers = _cross_file_clones(root_path, normalised, window_size)
    return _region_violations(root_path, normalised, starts, peers, window_size)


def _cross_file_clones(
    root_path: Path,
    normalised: dict[Path, list[_NormLine]],
    window_size: int,
) -> tuple[dict[Path, list[int]], dict[Path, set[str]]]:
    index: dict[str, list[tuple[Path, int]]] = defaultdict(list)
    for path, lines in normalised.items():
        if len(lines) < window_size:
            continue
        for start in range(0, len(lines) - window_size + 1):
            index[_window_digest(lines, start, window_size)].append((path, start))
    return _collect_clone_hits(root_path, index)


def _collect_clone_hits(
    root_path: Path,
    index: dict[str, list[tuple[Path, int]]],
) -> tuple[dict[Path, list[int]], dict[Path, set[str]]]:
    clone_starts: dict[Path, list[int]] = defaultdict(list)
    clone_peers: dict[Path, set[str]] = defaultdict(set)
    for locations in index.values():
        paths = {path for path, _ in locations}
        if len(paths) < 2:
            continue
        for path, start in locations:
            clone_starts[path].append(start)
            clone_peers[path].update(relative_to_repo(root_path, other) for other in paths if other != path)
    return clone_starts, clone_peers


def _region_violations(
    root_path: Path,
    normalised: dict[Path, list[_NormLine]],
    clone_starts: dict[Path, list[int]],
    clone_peers: dict[Path, set[str]],
    window_size: int,
) -> list[DuplicateViolation]:
    violations: list[DuplicateViolation] = []
    for path, starts in clone_starts.items():
        lines = normalised[path]
        peers = tuple(sorted(clone_peers[path]))
        rel = relative_to_repo(root_path, path)
        for begin, end, value in _merge_window_starts(sorted(starts), window_size):
            violations.append(
                DuplicateViolation(
                    path=rel,
                    start_line=lines[begin].lineno,
                    end_line=lines[min(end, len(lines) - 1)].lineno,
                    value=value,
                    peers=peers,
                )
            )
    return sorted(violations, key=lambda item: (-item.value, item.path, item.start_line))


@dataclass(frozen=True)
class _NormLine:
    lineno: int
    text: str


def _normalise_file(path: Path) -> list[_NormLine]:
    result: list[_NormLine] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        result.append(_NormLine(lineno=lineno, text=" ".join(stripped.split())))
    return result


def _window_digest(lines: list[_NormLine], start: int, window_size: int) -> str:
    payload = "\n".join(item.text for item in lines[start : start + window_size]).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _merge_window_starts(starts: list[int], window_size: int) -> list[tuple[int, int, int]]:
    """Merge overlapping/adjacent windows into regions; value is normalised line count."""

    if not starts:
        return []
    regions: list[tuple[int, int, int]] = []
    begin = prev = starts[0]
    for start in starts[1:]:
        if start <= prev + 1:
            prev = start
            continue
        last_index = prev + window_size - 1
        regions.append((begin, last_index, last_index - begin + 1))
        begin = prev = start
    last_index = prev + window_size - 1
    regions.append((begin, last_index, last_index - begin + 1))
    return regions
