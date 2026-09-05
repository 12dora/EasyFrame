"""跨文件重复检测:规范化 40 行窗口。"""

from __future__ import annotations

from pathlib import Path

from tools.quality_gates.code_size import ScanSpec
from tools.quality_gates.duplicates import WINDOW_SIZE, scan_duplicate_windows


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
