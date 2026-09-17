"""先执行唯一 owner 的 Alembic migration，再启动 blank backend。"""

import os

import uvicorn
from alembic import command
from alembic.config import Config


def web_workers() -> int:
    """解析 Web worker 数；无效配置在启动迁移前直接失败。"""

    raw = os.getenv("BLANK_WEB_WORKERS", "2")
    try:
        workers = int(raw)
    except ValueError as exc:
        raise RuntimeError("BLANK_WEB_WORKERS must be an integer greater than or equal to 1") from exc
    if workers < 1:
        raise RuntimeError("BLANK_WEB_WORKERS must be an integer greater than or equal to 1")
    return workers


def main() -> None:
    config = Config(os.path.join(os.path.dirname(__file__), "alembic.ini"))
    command.upgrade(config, "head")
    uvicorn.run(
        "blank_app.main:app",
        host=os.getenv("BLANK_HOST", "0.0.0.0"),
        port=int(os.getenv("BLANK_PORT", "8000")),
        workers=web_workers(),
    )


if __name__ == "__main__":
    main()
