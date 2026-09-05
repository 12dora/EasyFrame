"""跨文件重复检测:规范化 40 行窗口。"""

from __future__ import annotations

from pathlib import Path

from tools.quality_gates.code_size import ScanSpec
from tools.quality_gates.duplicates import WINDOW_SIZE, scan_duplicate_windows
from tools.quality_gates.runner import collect_duplicates, compare_findings, sort_findings


def _write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _block(prefix: str, count: int = WINDOW_SIZE) -> list[str]:
    return [f"{prefix}_{index} = {index}" for index in range(count)]


def test_duplicate_detector_reports_cross_file_40_line_clone(tmp_path: Path) -> None:
    clone = _block("ITEM", 45)
    _write(tmp_path / "src/alpha.py", ["# header", "", *clone, "ALPHA = 1"])
    _write(tmp_path / "src/beta.py", ["BETA = 0", *clone, "# trailing"])

    violations = scan_duplicate_windows(tmp_path, ScanSpec(roots=("src",)))

    by_path = {item.path: item for item in violations}
    assert set(by_path) == {"src/alpha.py", "src/beta.py"}
    assert by_path["src/alpha.py"].value == 45
    assert by_path["src/beta.py"].value == 45
    assert "src/beta.py" in by_path["src/alpha.py"].peers


def test_duplicate_detector_ignores_comment_and_whitespace_differences(tmp_path: Path) -> None:
    left = [*_block("SHARED"), "LEFT = 1"]
    right = ["# only comments differ", *[f"  {line}  " for line in _block("SHARED")], "RIGHT = 1"]
    _write(tmp_path / "src/left.py", left)
    _write(tmp_path / "src/right.py", right)

    violations = scan_duplicate_windows(tmp_path, ScanSpec(roots=("src",)))

    assert {item.path for item in violations} == {"src/left.py", "src/right.py"}
    assert all(item.value == WINDOW_SIZE for item in violations)


def test_duplicate_detector_does_not_flag_single_file_or_short_windows(tmp_path: Path) -> None:
    clone = _block("SOLO", WINDOW_SIZE)
    short = _block("TINY", WINDOW_SIZE - 1)
    _write(tmp_path / "src/only.py", clone)
    _write(tmp_path / "src/a.py", short)
    _write(tmp_path / "src/b.py", short)

    assert scan_duplicate_windows(tmp_path, ScanSpec(roots=("src",))) == []


def test_duplicate_detector_preserves_string_literal_whitespace(tmp_path: Path) -> None:
    left = [f'label_{index} = "hello  world"' for index in range(WINDOW_SIZE)]
    right = [f'label_{index} = "hello world"' for index in range(WINDOW_SIZE)]
    _write(tmp_path / "src/left.py", left)
    _write(tmp_path / "src/right.py", right)

    assert scan_duplicate_windows(tmp_path, ScanSpec(roots=("src",))) == []


def test_duplicate_detector_skips_import_decorator_and_field_windows(tmp_path: Path) -> None:
    imports = [f"import package_{index}" for index in range(WINDOW_SIZE)]
    decorators = [f"@decorate_{index}" for index in range(WINDOW_SIZE)]
    fields = [f"field_{index}: int" for index in range(WINDOW_SIZE)]
    for name, lines in ("imports", imports), ("decorators", decorators), ("fields", fields):
        _write(tmp_path / f"src/{name}_a.py", lines)
        _write(tmp_path / f"src/{name}_b.py", lines)

    assert scan_duplicate_windows(tmp_path, ScanSpec(roots=("src",))) == []


def test_splitting_a_duplicate_region_is_not_a_regression(tmp_path: Path) -> None:
    clone = _block("CHUNK", 90)
    _write(tmp_path / "src/alpha.py", clone)
    _write(tmp_path / "src/beta.py", clone)
    spec = ScanSpec(roots=("src",))
    before = sort_findings(collect_duplicates(tmp_path, spec, WINDOW_SIZE))

    edited = list(clone)
    edited[45] = "CHUNK_45 = 'split-here'"
    _write(tmp_path / "src/alpha.py", edited)
    after = sort_findings(collect_duplicates(tmp_path, spec, WINDOW_SIZE))

    regressions, _improvements = compare_findings(after, before)
    assert regressions == []
    assert before["duplicate-window"]["src/alpha.py"] == [90]
    assert after["duplicate-window"]["src/alpha.py"] == [89]
    assert len(after["duplicate-window"]["src/alpha.py"]) == 1
