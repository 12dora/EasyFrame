"""blank host 数据库连接；正式默认只使用 PostgreSQL。"""

import os

from sqlalchemy import URL, create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.getenv("BLANK_DATABASE_URL", "")
if not DATABASE_URL and os.getenv("BLANK_POSTGRES_PASSWORD"):
    DATABASE_URL = URL.create(
        "postgresql+psycopg",
        username=os.getenv("BLANK_POSTGRES_USER", "enterprise"),
        password=os.environ["BLANK_POSTGRES_PASSWORD"],
        host=os.getenv("BLANK_POSTGRES_HOST", "blank-postgres"),
        port=int(os.getenv("BLANK_POSTGRES_PORT", "5432")),
        database=os.getenv("BLANK_POSTGRES_DB", "enterprise_blank"),
    ).render_as_string(hide_password=False)
if not DATABASE_URL:
    raise RuntimeError("BLANK_DATABASE_URL is required")
if not DATABASE_URL.startswith(("postgresql://", "postgresql+psycopg://")):
    raise RuntimeError("BLANK_DATABASE_URL must be a PostgreSQL URL")

# 宿主性能:加大连接池并回收空闲连接,保留 pre_ping。
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    pool_recycle=1800,
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class BlankBase(DeclarativeBase):
    pass
