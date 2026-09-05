"""代码气味门禁:ruff + 体积 + 跨文件重复,对照只缩不涨的 JSON 基线。"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from tools.quality_gates.code_size import (
    PYTHON_FILE_KIND,
    PYTHON_FUNCTION_KIND,
    TEXT_FILE_KIND,
    CodeSizeThresholds,
    ScanSpec,
    scan_code_size_violations,
)
from tools.quality_gates.duplicates import WINDOW_SIZE, scan_duplicate_windows

RUFF_VALUE_RE = re.compile(r"\((\d+) > \d+\)")
SIZE_RULES = {
    PYTHON_FILE_KIND: "file-sloc",
    PYTHON_FUNCTION_KIND: "function-sloc",
    TEXT_FILE_KIND: "fixture-sloc",
}
DUPLICATE_RULE = "duplicate-window"
BASELINE_COMMENT = "代码气味存量基线,只允许变小。用 python -m tools.quality_gates.runner --update 收紧。"
Findings = dict[str, dict[str, list[int]]]


def load_gates_config(path: Path, repo_root: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"gates 配置必须是 JSON 对象: {path}")
    payload["_config_path"] = path
    payload["_repo_root"] = repo_root
    return payload


def scan_spec_from_config(payload: dict[str, Any]) -> ScanSpec:
    return ScanSpec(
        roots=tuple(payload.get("scan_roots") or ()),
        test_substrings=tuple(payload.get("test_substrings") or ScanSpec.test_substrings),
        route_substrings=tuple(payload.get("route_substrings") or ScanSpec.route_substrings),
        route_exclude_substrings=tuple(payload.get("route_exclude_substrings") or ScanSpec.route_exclude_substrings),
        migration_substrings=tuple(payload.get("migration_substrings") or ScanSpec.migration_substrings),
        fixture_globs=tuple(payload.get("fixture_globs") or ScanSpec.fixture_globs),
    )


def thresholds_from_config(payload: dict[str, Any]) -> CodeSizeThresholds:
    raw = payload.get("thresholds") or {}
    defaults = CodeSizeThresholds()
    return CodeSizeThresholds(
        production_file=int(raw.get("production_file", defaults.production_file)),
        route_file=int(raw.get("route_file", defaults.route_file)),
        test_file=int(raw.get("test_file", defaults.test_file)),
        fixture_file=int(raw.get("fixture_file", defaults.fixture_file)),
        function=int(raw.get("function", defaults.function)),
        migration=int(raw.get("migration", defaults.migration)),
        test_function=int(raw.get("test_function", defaults.test_function)),
    )


def collect_findings(repo_root: Path, payload: dict[str, Any]) -> Findings:
    spec = scan_spec_from_config(payload)
    found: Findings = defaultdict(dict)
    _merge(found, collect_ruff(repo_root, payload))
    _merge(found, collect_size(repo_root, spec, thresholds_from_config(payload)))
    _merge(
        found,
        collect_duplicates(repo_root, spec, int(payload.get("duplicate_window") or WINDOW_SIZE)),
    )
    return sort_findings(found)


def collect_ruff(repo_root: Path, payload: dict[str, Any]) -> Findings:
    command = list(payload.get("ruff_command") or ["uv", "tool", "run", "ruff@0.16.2"])
    config = repo_root / payload.get("ruff_config", "backend/pyproject.toml")
    roots = [str(repo_root / item) for item in payload.get("scan_roots") or []]
    proc = subprocess.run(
        [*command, "check", "--output-format", "json", "--config", str(config), *roots],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode not in (0, 1):
        sys.stderr.write(proc.stdout[-4000:] + proc.stderr[-4000:])
        raise SystemExit(f"ruff 执行失败(exit {proc.returncode})")
    try:
        diagnostics = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        sys.stderr.write(proc.stdout[-4000:] + proc.stderr[-4000:])
        raise SystemExit("ruff 没有产出可解析的 JSON") from None
    found: Findings = defaultdict(lambda: defaultdict(list))
    for item in diagnostics:
        code = item.get("code")
        filename = item.get("filename")
        if not code or not filename:
            continue
        rel = _repo_relative(repo_root, Path(filename))
        found[code][rel].append(_ruff_value(item.get("message") or ""))
    return _freeze_lists(found)


def collect_size(repo_root: Path, spec: ScanSpec, thresholds: CodeSizeThresholds) -> Findings:
    found: Findings = defaultdict(lambda: defaultdict(list))
    for violation in scan_code_size_violations(repo_root, spec, thresholds):
        found[SIZE_RULES[violation.kind]][violation.path].append(violation.sloc)
    return _freeze_lists(found)


def collect_duplicates(repo_root: Path, spec: ScanSpec, window_size: int) -> Findings:
    found: Findings = defaultdict(lambda: defaultdict(list))
    for violation in scan_duplicate_windows(repo_root, spec, window_size=window_size):
        found[DUPLICATE_RULE][violation.path].append(violation.value)
    return _freeze_lists(found)


def sort_findings(found: Findings) -> Findings:
    ordered: Findings = {}
    for rule in sorted(found):
        files = {path: sorted(values, reverse=True) for path, values in sorted(found[rule].items()) if values}
        if files:
            ordered[rule] = files
    return ordered


def load_baseline(path: Path) -> Findings:
    if not path.exists():
        raise SystemExit(f"缺少基线文件 {path};先跑 runner --update 生成")
    payload = json.loads(path.read_text(encoding="utf-8"))
    files = payload.get("rules", payload)
    if not isinstance(files, dict):
        raise SystemExit(f"基线格式无效: {path}")
    found: Findings = {}
    for rule, entries in files.items():
        if rule.startswith("_") or not isinstance(entries, dict):
            continue
        found[rule] = {path: sorted((int(value) for value in values), reverse=True) for path, values in entries.items()}
    return sort_findings(found)


def write_baseline(path: Path, found: Findings) -> None:
    payload = {"_comment": BASELINE_COMMENT, "rules": found}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def compare_findings(found: Findings, baseline: Findings) -> tuple[list[str], list[str]]:
    """对照基线:新违规或数值变差 → 红;变好 → 绿并可 --update 收紧。"""

    regressions: list[str] = []
    improvements: list[str] = []
    rules = sorted(set(found) | set(baseline))
    for rule in rules:
        files = sorted(set(found.get(rule, ())) | set(baseline.get(rule, ())))
        for path in files:
            now = found.get(rule, {}).get(path, [])
            was = baseline.get(rule, {}).get(path, [])
            if len(now) > len(was):
                regressions.append(f"{path} [{rule}] 违规 {len(was)} -> {len(now)} 条;当前数值 {now}")
                continue
            worse = [(old, new) for old, new in zip(was, now, strict=False) if new > old]
            if worse:
                regressions.append(f"{path} [{rule}] 既存违规继续膨胀:{worse}(基线 {was} -> 当前 {now})")
            elif len(now) < len(was) or now != was:
                improvements.append(f"{path} [{rule}] {was} -> {now}")
    return regressions, improvements


def grow_reasons(found: Findings, baseline: Findings) -> list[str]:
    """--update 只允许收缩;返回拒绝涨基线的原因。"""

    reasons, _improvements = compare_findings(found, baseline)
    return reasons


def format_table(found: Findings, baseline: Findings, regressions: list[str], improvements: list[str]) -> str:
    improved_rules = {item.split(" [", 1)[1].split("]", 1)[0] for item in improvements}
    regressed_rules = {item.split(" [", 1)[1].split("]", 1)[0] for item in regressions}
    rules = sorted(set(found) | set(baseline))
    rows = ["RULE                 FILES  COUNT  STATUS"]
    for rule in rules:
        current = found.get(rule, {})
        count = sum(len(values) for values in current.values())
        file_count = len(current)
        status = "ok"
        if rule in regressed_rules:
            status = "REGRESSED"
        elif rule in improved_rules:
            status = "improved; tighten with --update"
        elif count:
            status = "frozen"
        rows.append(f"{rule:<20} {file_count:>5}  {count:>5}  {status}")
    if not rules:
        rows.append(f"{'(none)':<20} {0:>5}  {0:>5}  ok")
    return "\n".join(rows)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path, help="仓库根目录")
    parser.add_argument("--config", required=True, type=Path, help="gates.json")
    parser.add_argument("--update", action="store_true", help="用当前结果收紧基线(只允许变小)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = args.repo.resolve()
    config_path = args.config if args.config.is_absolute() else Path.cwd() / args.config
    payload = load_gates_config(config_path, repo_root)
    found = collect_findings(repo_root, payload)
    baseline_path = repo_root / payload.get("baseline", "backend/.code-smells-baseline.json")
    total = sum(len(values) for files in found.values() for values in files.values())

    if args.update:
        return _run_update(found, baseline_path, total)

    baseline = load_baseline(baseline_path)
    regressions, improvements = compare_findings(found, baseline)
    print(format_table(found, baseline, regressions, improvements))
    print(f"\n{len(found)} rules / {total} violations vs baseline {baseline_path.relative_to(repo_root)}")
    if regressions:
        print(f"\nFAILED ({len(regressions)} regressions):", file=sys.stderr)
        for line in regressions:
            print(f"  - {line}", file=sys.stderr)
        print("\n拆函数/删重复,不要抬阈值或加 noqa;基线只在变好之后用 --update 收紧。", file=sys.stderr)
        return 1
    if improvements:
        print(f"\n{len(improvements)} better than baseline; tighten with --update:")
        for line in improvements[:20]:
            print(f"  - {line}")
        if len(improvements) > 20:
            print(f"  ... {len(improvements) - 20} more")
    return 0


def _run_update(found: Findings, baseline_path: Path, total: int) -> int:
    if baseline_path.exists():
        reasons = grow_reasons(found, load_baseline(baseline_path))
        if reasons:
            print("拒绝写入:基线只允许收缩,当前结果比基线更差:", file=sys.stderr)
            for line in reasons:
                print(f"  - {line}", file=sys.stderr)
            return 1
    write_baseline(baseline_path, found)
    print(f"baseline updated: {len(found)} rules / {total} violations -> {baseline_path}")
    return 0


def _ruff_value(message: str) -> int:
    match = RUFF_VALUE_RE.search(message)
    return int(match.group(1)) if match else 1


def _repo_relative(repo_root: Path, path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(repo_root).as_posix()
    except ValueError:
        return resolved.as_posix()


def _merge(target: Findings, extra: Findings) -> None:
    for rule, files in extra.items():
        bucket = target.setdefault(rule, {})
        for path, values in files.items():
            bucket.setdefault(path, []).extend(values)


def _freeze_lists(found: Findings) -> Findings:
    return {
        rule: {path: sorted(values, reverse=True) for path, values in files.items()} for rule, files in found.items()
    }


if __name__ == "__main__":
    raise SystemExit(main())
