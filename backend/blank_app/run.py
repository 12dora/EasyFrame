"""先执行唯一 owner 的 Alembic migration，再启动 blank backend。"""

import os

import uvicorn
from alembic.config import Config

from alembic import command


def main() -> None:
    config = Config(os.path.join(os.path.dirname(__file__), "alembic.ini"))
    command.upgrade(config, "head")
    uvicorn.run(
        "blank_app.main:app",
        host=os.getenv("BLANK_HOST", "0.0.0.0"),
        port=int(os.getenv("BLANK_PORT", "8000")),
    )


if __name__ == "__main__":
    main()
