"""棘轮:基线只缩不涨。"""

from __future__ import annotations

import json
from pathlib import Path

from tools.quality_gates.runner import (
    compare_findings,
    grow_reasons,
    load_baseline,
    sort_findings,
    write_baseline,
)


def test_compare_findings_flags_new_and_worse_values() -> None:
    baseline = {"C901": {"a.py": [22, 15]}}
    found = {"C901": {"a.py": [23, 15], "b.py": [12]}}

    regressions, improvements = compare_findings(found, baseline)

    assert any("继续膨胀" in item and "a.py" in item for item in regressions)
    assert any("b.py" in item and "1 -> 1" not in item for item in regressions)
    assert improvements == []


def test_compare_findings_treats_shrink_as_improvement() -> None:
    baseline = {"C901": {"a.py": [22, 15]}, "file-sloc": {"b.py": [612]}}
    found = {"C901": {"a.py": [15]}}

    regressions, improvements = compare_findings(found, baseline)

    assert regressions == []
    assert len(improvements) == 2


def test_update_refuses_to_grow(tmp_path: Path) -> None:
    baseline_path = tmp_path / ".code-smells-baseline.json"
    write_baseline(baseline_path, {"C901": {"a.py": [20]}})
    found = {"C901": {"a.py": [21]}}

    reasons = grow_reasons(found, load_baseline(baseline_path))

    assert reasons
    loaded = load_baseline(baseline_path)
    assert loaded == {"C901": {"a.py": [20]}}


def test_update_allows_shrink_and_equal_rewrite(tmp_path: Path) -> None:
    baseline_path = tmp_path / ".code-smells-baseline.json"
    write_baseline(baseline_path, {"C901": {"a.py": [22, 15]}, "PLR0915": {"a.py": [50]}})
    shrunk = sort_findings({"C901": {"a.py": [15]}})

    assert grow_reasons(shrunk, load_baseline(baseline_path)) == []
    write_baseline(baseline_path, shrunk)
    payload = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert payload["rules"] == {"C901": {"a.py": [15]}}
    assert "PLR0915" not in payload["rules"]
