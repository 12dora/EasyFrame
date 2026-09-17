"""Alembic env.py 不得用 fileConfig 默认值禁用进程内已存在的应用 logger。"""

from __future__ import annotations

import ast
import logging
from logging.config import fileConfig
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
_ENV_PY = _BACKEND / "blank_app" / "alembic" / "env.py"
_ALEMBIC_INI = _BACKEND / "blank_app" / "alembic.ini"
_APP_LOGGER = "enterprise_platform.authz.snapshot_freshness"


def _fileconfig_kwargs(env_path: Path) -> dict[str, object]:
    tree = ast.parse(env_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "fileConfig":
            continue
        return {kw.arg: ast.literal_eval(kw.value) for kw in node.keywords if kw.arg}
    raise AssertionError(f"fileConfig() missing in {env_path}")


def test_alembic_file_config_does_not_disable_existing_app_loggers() -> None:
    kwargs = _fileconfig_kwargs(_ENV_PY)
    assert kwargs.get("disable_existing_loggers") is False

    probe = logging.getLogger(_APP_LOGGER)
    probe.info("pre-fileConfig")
    assert probe.disabled is False

    fileConfig(str(_ALEMBIC_INI), **kwargs)
    assert probe.disabled is False
