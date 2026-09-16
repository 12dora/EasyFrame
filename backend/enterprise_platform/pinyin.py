"""姓名拼音:全拼与首字母、ORM 列填充、列表 ILIKE。"""

from __future__ import annotations

import re
from typing import Any, Final

from pypinyin import Style, lazy_pinyin
from sqlalchemy import String, or_
from sqlalchemy.orm import Mapped, mapped_column, validates
from sqlalchemy.sql.elements import ColumnElement

_HAN_RUN: Final = re.compile(r"[一-鿿㐀-䶿]+")
_MAX: Final = 128


def pinyin_full(name: str) -> str:
    """小写全拼,仅字母数字、无分隔;超长截到 128。"""

    return _fields(name)[0]


def pinyin_initials(name: str) -> str:
    """拼音首字母(非汉字片段整段保留);超长截到 128。"""

    return _fields(name)[1]


def pinyin_search_term(keyword: str) -> str | None:
    """去空白后若为纯 ASCII 字母数字,返回小写词;否则不按拼音列匹配。"""

    term = keyword.lower().replace(" ", "")
    if term and term.isascii() and term.isalnum():
        return term
    return None


def matches_person_query(query: str, *, name: str, name_pinyin: str = "", name_pinyin_initials: str = "") -> bool:
    """内存侧与目录 ``q`` 相同:姓名子串,或纯字母数字查询匹配全拼/首字母。"""

    keyword = query.strip()
    if not keyword:
        return True
    if keyword.lower() in (name or "").lower():
        return True
    term = pinyin_search_term(keyword)
    if term is None:
        return False
    return term in (name_pinyin or "").lower() or term in (name_pinyin_initials or "").lower()


def _fields(name: str) -> tuple[str, str]:
    full: list[str] = []
    initials: list[str] = []
    text = name or ""
    cursor = 0
    for match in _HAN_RUN.finditer(text):
        _append_literal(text[cursor : match.start()], full, initials)
        _append_han(match.group(), full, initials)
        cursor = match.end()
    _append_literal(text[cursor:], full, initials)
    return "".join(full)[:_MAX], "".join(initials)[:_MAX]


def _append_han(run: str, full: list[str], initials: list[str]) -> None:
    for syllable in lazy_pinyin(run, style=Style.NORMAL):
        letters = _ascii_alnum(syllable)
        if not letters:
            continue
        full.append(letters)
        initials.append(letters[0])


def _append_literal(run: str, full: list[str], initials: list[str]) -> None:
    letters = _ascii_alnum(run)
    if not letters:
        return
    full.append(letters)
    initials.append(letters)


def _ascii_alnum(value: str) -> str:
    return "".join(ch.lower() for ch in value if ch.isascii() and ch.isalnum())


class PinyinNameMixin:
    """姓名变更时同步全拼 / 首字母;ORM 写入路径无需各处手填。"""

    name_pinyin: Mapped[str] = mapped_column(String(128), default="", server_default="", index=True)
    name_pinyin_initials: Mapped[str] = mapped_column(String(128), default="", server_default="", index=True)

    @validates("name")
    def _fill_name_pinyin(self, _key: str, value: str | None) -> str:
        name = value or ""
        self.name_pinyin = pinyin_full(name)
        self.name_pinyin_initials = pinyin_initials(name)
        return name


def escape_like(term: str) -> str:
    """转义 ``\\`` ``%`` ``_``,供 ``escape='\\'`` 的 LIKE / ILIKE 使用。"""

    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def ilike_contains(column: ColumnElement[Any], term: str) -> ColumnElement[bool]:
    """不区分大小写的子串匹配;通配符按字面量。"""

    return column.ilike(f"%{escape_like(term)}%", escape="\\")


def person_name_match(
    keyword: str,
    name_col: ColumnElement[Any],
    pinyin_col: ColumnElement[Any],
    initials_col: ColumnElement[Any],
) -> ColumnElement[bool]:
    """姓名子串,或纯字母数字查询再匹配全拼 / 首字母。"""

    clauses = [ilike_contains(name_col, keyword)]
    term = pinyin_search_term(keyword)
    if term:
        clauses.append(ilike_contains(pinyin_col, term))
        clauses.append(ilike_contains(initials_col, term))
    return or_(*clauses)


__all__ = [
    "PinyinNameMixin",
    "escape_like",
    "ilike_contains",
    "matches_person_query",
    "person_name_match",
    "pinyin_full",
    "pinyin_initials",
    "pinyin_search_term",
]
