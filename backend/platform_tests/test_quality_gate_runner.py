"""棘轮:基线只缩不涨。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tools.quality_gates.runner import (
    _run_update,
    collect_findings,
    collect_ruff,
    compare_findings,
    grow_reasons,
    load_baseline,
    load_gates_config,
    main,
    sort_findings,
    write_baseline,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]
FRAGMENT = BACKEND_DIR / "ruff-gates.toml"
RUFF = ["uv", "tool", "run", "ruff@0.16.2"]
DEPENDS_SOURCE = "def Depends():\n    return None\n\ndef endpoint(user=Depends()):\n    return user\n"


def test_compare_findings_flags_new_and_worse_values() -> None:
    baseline = {"C901": {"a.py::foo": [22], "a.py::bar": [15]}}
    found = {"C901": {"a.py::foo": [23], "a.py::bar": [15], "b.py::baz": [12]}}

    regressions, improvements = compare_findings(found, baseline)

    assert any("继续膨胀" in item and "a.py::foo" in item for item in regressions)
    assert any("b.py::baz" in item for item in regressions)
    assert improvements == []


def test_compare_findings_treats_shrink_as_improvement() -> None:
    baseline = {"C901": {"a.py::foo": [22], "a.py::bar": [15]}, "file-sloc": {"b.py": [612]}}
    found = {"C901": {"a.py::bar": [15]}}

    regressions, improvements = compare_findings(found, baseline)

    assert regressions == []
    assert any("a.py::foo" in item for item in improvements)
    assert any("b.py" in item for item in improvements)


def test_function_ratchet_does_not_let_one_symbol_consume_another_allowance() -> None:
    baseline = {"C901": {"a.py::A": [22], "a.py::B": [15]}}
    found = {"C901": {"a.py::A": [15], "a.py::B": [20]}}

    regressions, improvements = compare_findings(found, baseline)

    assert any("a.py::B" in item and "继续膨胀" in item for item in regressions)
    assert any("a.py::A" in item for item in improvements)
    assert not any("a.py::A" in item for item in regressions)


def test_deleted_symbol_is_not_a_regression() -> None:
    baseline = {"C901": {"a.py::A": [22], "a.py::B": [15]}}
    found = {"C901": {"a.py::B": [15]}}

    regressions, improvements = compare_findings(found, baseline)

    assert regressions == []
    assert any("a.py::A" in item and "-> []" in item for item in improvements)


def test_new_symbol_over_threshold_is_a_regression() -> None:
    baseline = {"C901": {"a.py::A": [22]}}
    found = {"C901": {"a.py::A": [22], "a.py::C": [12]}}

    regressions, improvements = compare_findings(found, baseline)

    assert any("a.py::C" in item for item in regressions)
    assert improvements == []


def test_run_update_rejects_growth_leaves_file(tmp_path: Path) -> None:
    baseline_path = tmp_path / ".code-smells-baseline.json"
    original = {"C901": {"a.py::foo": [20]}}
    write_baseline(baseline_path, original)
    before = baseline_path.read_text(encoding="utf-8")

    rc = _run_update({"C901": {"a.py::foo": [21]}}, baseline_path, 1)

    assert rc == 1
    assert baseline_path.read_text(encoding="utf-8") == before
    assert load_baseline(baseline_path) == original


def test_run_update_accepts_shrink_and_rewrites(tmp_path: Path) -> None:
    baseline_path = tmp_path / ".code-smells-baseline.json"
    write_baseline(baseline_path, {"C901": {"a.py::foo": [22], "a.py::bar": [15]}, "PLR0915": {"a.py::foo": [50]}})
    shrunk = sort_findings({"C901": {"a.py::bar": [15]}})

    rc = _run_update(shrunk, baseline_path, 1)

    assert rc == 0
    payload = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert payload["rules"] == {"C901": {"a.py::bar": [15]}}
    assert "PLR0915" not in payload["rules"]


def test_cli_update_rejects_growth_and_accepts_shrink(tmp_path: Path, monkeypatch) -> None:
    repo, config_path, baseline_path = _cli_repo(tmp_path)
    original = {"C901": {"a.py::foo": [20]}}
    write_baseline(baseline_path, original)
    before = baseline_path.read_text(encoding="utf-8")
    import tools.quality_gates.runner as runner_mod

    monkeypatch.setattr(runner_mod, "collect_findings", lambda *_args, **_kwargs: {"C901": {"a.py::foo": [21]}})
    assert main(["--repo", str(repo), "--config", str(config_path), "--update"]) == 1
    assert baseline_path.read_text(encoding="utf-8") == before

    monkeypatch.setattr(runner_mod, "collect_findings", lambda *_args, **_kwargs: {"C901": {"a.py::foo": [15]}})
    assert main(["--repo", str(repo), "--config", str(config_path), "--update"]) == 0
    assert load_baseline(baseline_path) == {"C901": {"a.py::foo": [15]}}


def test_legacy_file_baseline_migrates_with_update(tmp_path: Path) -> None:
    baseline_path = tmp_path / ".code-smells-baseline.json"
    write_baseline(baseline_path, {"C901": {"a.py": [22, 15]}})
    found = sort_findings({"C901": {"a.py::A": [22], "a.py::B": [15]}})

    assert grow_reasons(found, load_baseline(baseline_path)) == []
    assert _run_update(found, baseline_path, 2) == 0
    assert load_baseline(baseline_path) == found


def test_fragment_api_depends_needs_downstream_per_file_ignore(tmp_path: Path) -> None:
    repo = tmp_path / "downstream"
    api_file = repo / "api" / "x.py"
    api_file.parent.mkdir(parents=True)
    api_file.write_text(DEPENDS_SOURCE, encoding="utf-8")
    pyproject = repo / "pyproject.toml"
    pyproject.write_text(_downstream_pyproject(), encoding="utf-8")

    without_ignore = _ruff_json(repo, pyproject, api_file)
    assert any(item.get("code") == "B008" for item in without_ignore)

    pyproject.write_text(
        _downstream_pyproject(
            """
[tool.ruff.lint.extend-per-file-ignores]
"app/api/v1/**/*.py" = ["B008", "PLR0913"]
"api/**/*.py" = ["B008", "PLR0913"]
"""
        ),
        encoding="utf-8",
    )
    with_ignore = _ruff_json(repo, pyproject, api_file)
    assert not any(item.get("code") == "B008" for item in with_ignore)


def test_runner_results_are_identical_from_different_cwds(tmp_path: Path, monkeypatch) -> None:
    repo = _host_repo_with_api_ignore(tmp_path)
    payload = load_gates_config(repo / "gates.json", repo)
    cwd_a = repo
    cwd_b = tmp_path / "other-cwd"
    cwd_b.mkdir()

    monkeypatch.chdir(cwd_a)
    from_repo_root = collect_findings(repo, payload)
    monkeypatch.chdir(cwd_b)
    from_elsewhere = collect_findings(repo, payload)

    assert from_repo_root == from_elsewhere
    ruff_hits = collect_ruff(repo, payload)
    b008_keys = set(ruff_hits.get("B008", ()))
    assert "backend/app/other.py" in b008_keys
    assert "backend/app/api/x.py" not in b008_keys

    codes = []
    for cwd in (cwd_a, cwd_b):
        monkeypatch.chdir(cwd)
        codes.append(main(["--repo", str(repo), "--config", str(repo / "gates.json")]))
    assert codes == [0, 0]


def _cli_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "host"
    backend = repo / "backend"
    backend.mkdir(parents=True)
    config_path = repo / "gates.json"
    config_path.write_text(
        json.dumps(
            {
                "scan_roots": ["backend"],
                "baseline": "backend/.code-smells-baseline.json",
                "ruff_config": "backend/pyproject.toml",
            }
        ),
        encoding="utf-8",
    )
    return repo, config_path, backend / ".code-smells-baseline.json"


def _downstream_pyproject(extra: str = "") -> str:
    return f"""
[tool.ruff]
extend = "{FRAGMENT.resolve().as_posix()}"
target-version = "py311"
line-length = 120
{extra}
"""


def _ruff_json(cwd: Path, config: Path, *paths: Path) -> list[dict]:
    proc = subprocess.run(
        [
            *RUFF,
            "check",
            "--select",
            "B008",
            "--output-format",
            "json",
            "--config",
            str(config),
            *[str(p) for p in paths],
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
    )
    if proc.returncode not in (0, 1):
        raise AssertionError(proc.stdout + proc.stderr)
    return json.loads(proc.stdout or "[]")


def _host_repo_with_api_ignore(tmp_path: Path) -> Path:
    repo = tmp_path / "host"
    backend = repo / "backend"
    api = backend / "app" / "api"
    api.mkdir(parents=True)
    (api / "x.py").write_text(DEPENDS_SOURCE, encoding="utf-8")
    (backend / "app" / "other.py").write_text(DEPENDS_SOURCE, encoding="utf-8")
    (backend / "pyproject.toml").write_text(
        _downstream_pyproject(
            """
[tool.ruff.lint.extend-per-file-ignores]
"app/api/**/*.py" = ["B008", "PLR0913"]
"""
        ),
        encoding="utf-8",
    )
    found_payload = {
        "scan_roots": ["backend/app"],
        "baseline": "backend/.code-smells-baseline.json",
        "ruff_config": "backend/pyproject.toml",
        "ruff_command": RUFF,
        "duplicate_window": 40,
    }
    (repo / "gates.json").write_text(json.dumps(found_payload), encoding="utf-8")
    payload = load_gates_config(repo / "gates.json", repo)
    write_baseline(repo / "backend" / ".code-smells-baseline.json", collect_findings(repo, payload))
    return repo
