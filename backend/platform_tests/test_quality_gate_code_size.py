"""体积扫描器:文件/函数 SLOC 与分类阈值。"""

from __future__ import annotations

from pathlib import Path

from tools.quality_gates.code_size import (
    CodeSizeThresholds,
    ScanSpec,
    scan_code_size_violations,
    scan_python_file_size_violations,
    scan_python_function_size_violations,
    scan_text_file_size_violations,
)

DEFAULT_ROOTS = ("backend/app", "backend/alembic", "backend/app/tests")


def _spec(*roots: str) -> ScanSpec:
    return ScanSpec(roots=roots or DEFAULT_ROOTS)


def _write(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _python_lines(count: int) -> list[str]:
    return [f"VALUE_{index} = {index}" for index in range(count)]


def _function_source(name: str, body_lines: int) -> list[str]:
    return [f"def {name}():", *[f"    value_{index} = {index}" for index in range(body_lines)]]


def test_python_file_scanner_detects_backend_file_categories(tmp_path: Path) -> None:
    _write(tmp_path / "backend/app/api/v1/orders/demo.py", _python_lines(181))
    _write(tmp_path / "backend/app/seed_demo.py", _python_lines(501))
    _write(tmp_path / "backend/alembic/versions/q999_demo.py", _python_lines(501))
    _write(tmp_path / "backend/app/tests/test_big.py", _python_lines(401))

    violations = scan_python_file_size_violations(tmp_path, _spec(*DEFAULT_ROOTS))

    assert [(item.path, item.threshold) for item in violations] == [
        ("backend/alembic/versions/q999_demo.py", 500),
        ("backend/app/seed_demo.py", 500),
        ("backend/app/tests/test_big.py", 400),
        ("backend/app/api/v1/orders/demo.py", 180),
    ]
    assert {item.kind for item in violations} == {"python-file"}


def test_python_function_scanner_reports_nested_async_methods_and_source_ranges(tmp_path: Path) -> None:
    source = [
        "class Worker:",
        "    @transactional",
        "    async def run(self):",
        '        """Range includes this docstring, but SLOC does not."""',
        "        # comments do not count",
        "",
        "        async def inner():",
        *[f"            item_{index} = {index}" for index in range(40)],
        "        return await inner()",
    ]
    _write(tmp_path / "backend/app/domain/demo/service.py", source)

    violations = scan_python_function_size_violations(tmp_path, _spec("backend/app"))

    by_name = {item.qualifiedName: item for item in violations}
    assert set(by_name) == {"Worker.run", "Worker.run.inner"}
    assert by_name["Worker.run"].startLine == 2
    assert by_name["Worker.run"].endLine == len(source)
    assert by_name["Worker.run"].sloc == 44
    assert by_name["Worker.run"].threshold == 40
    assert by_name["Worker.run.inner"].startLine == 7
    assert by_name["Worker.run.inner"].sloc == 41


def test_python_function_scanner_counts_decorators_toward_symbol_sloc(tmp_path: Path) -> None:
    source = [
        *[f"@decorator_{index}" for index in range(45)],
        "def decorated():",
        "    value = 1",
        "    return value",
    ]
    _write(tmp_path / "backend/app/domain/demo/service.py", source)

    violations = scan_python_function_size_violations(tmp_path, _spec("backend/app"))

    assert [(item.qualifiedName, item.startLine, item.endLine, item.sloc, item.threshold) for item in violations] == [
        ("decorated", 1, len(source), 48, 40)
    ]


def test_text_fixture_and_alembic_function_thresholds_are_detected(tmp_path: Path) -> None:
    _write(tmp_path / "backend/app/tests/fixtures/large.json", ["{", *['  "x": 1,' for _ in range(399)], "}"])
    _write(tmp_path / "backend/alembic/versions/q999_big.py", _function_source("upgrade", 60))

    text_violations = scan_text_file_size_violations(tmp_path, _spec(*DEFAULT_ROOTS))
    function_violations = scan_python_function_size_violations(tmp_path, _spec(*DEFAULT_ROOTS))

    assert [(item.path, item.kind, item.sloc, item.threshold) for item in text_violations] == [
        ("backend/app/tests/fixtures/large.json", "text-file", 401, 400)
    ]
    assert [(item.qualifiedName, item.sloc, item.threshold) for item in function_violations] == [("upgrade", 61, 60)]


def test_new_oversized_item_fails_without_baseline(tmp_path: Path) -> None:
    _write(tmp_path / "backend/app/domain/demo/service.py", _function_source("new_large", 40))

    violations = scan_code_size_violations(tmp_path, _spec("backend/app"))

    assert [(item.path, item.kind, item.qualifiedName, item.sloc, item.threshold) for item in violations] == [
        ("backend/app/domain/demo/service.py", "python-function", "new_large", 41, 40)
    ]


def test_scan_roots_and_threshold_table_are_parameterised(tmp_path: Path) -> None:
    _write(tmp_path / "src/too_big.py", _python_lines(20))
    _write(tmp_path / "other/ignored.py", _python_lines(500))

    violations = scan_python_file_size_violations(
        tmp_path,
        ScanSpec(roots=("src",)),
        CodeSizeThresholds(production_file=10),
    )

    assert [(item.path, item.sloc, item.threshold) for item in violations] == [("src/too_big.py", 20, 10)]


def test_platform_tests_use_test_file_threshold(tmp_path: Path) -> None:
    _write(tmp_path / "backend/platform_tests/test_big.py", _python_lines(401))

    violations = scan_python_file_size_violations(tmp_path, ScanSpec(roots=("backend/platform_tests",)))

    assert [(item.path, item.threshold) for item in violations] == [("backend/platform_tests/test_big.py", 400)]
