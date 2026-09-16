"""姓名拼音转换、ORM 列填充与列表 ILIKE。"""

from __future__ import annotations

from sqlalchemy import Integer, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool

from enterprise_platform.pinyin import (
    PinyinNameMixin,
    ilike_contains,
    matches_person_query,
    person_name_match,
    pinyin_full,
    pinyin_initials,
    pinyin_search_term,
)


class _Base(DeclarativeBase):
    pass


class _Person(PinyinNameMixin, _Base):
    __tablename__ = "pinyin_people"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))


def test_pinyin_full_cjk() -> None:
    assert pinyin_full("张三") == "zhangsan"
    assert pinyin_initials("张三") == "zs"


def test_pinyin_mixed_cjk_and_latin() -> None:
    assert pinyin_full("胡玉琴A") == "huyuqina"
    assert pinyin_initials("胡玉琴A") == "hyqa"
    assert pinyin_full("Mike王") == "mikewang"
    assert pinyin_initials("Mike王") == "mikew"


def test_pinyin_empty() -> None:
    assert pinyin_full("") == ""
    assert pinyin_initials("") == ""
    assert pinyin_full(None) == ""  # type: ignore[arg-type]


def test_pinyin_long_truncated() -> None:
    name = "测" * 80
    assert len(pinyin_full(name)) == 128
    assert pinyin_full(name) == ("ce" * 80)[:128]
    assert pinyin_initials(name) == "c" * 80


def test_pinyin_skips_spaces_and_punctuation() -> None:
    assert pinyin_full("张 三-") == "zhangsan"
    assert pinyin_initials("张 三-") == "zs"


def test_pinyin_heteronyms_use_phrase_default() -> None:
    assert pinyin_full("重庆") == "chongqing"
    assert pinyin_initials("重庆") == "cq"
    assert pinyin_full("长安") == "changan"


def test_pinyin_search_term_letters_only() -> None:
    assert pinyin_search_term("Hyq") == "hyq"
    assert pinyin_search_term("Hu Yu") == "huyu"
    assert pinyin_search_term("胡") is None
    assert pinyin_search_term("") is None
    assert pinyin_search_term("-") is None


def test_matches_person_query_name_and_pinyin() -> None:
    person = {"name": "胡玉琴A", "name_pinyin": "huyuqina", "name_pinyin_initials": "hyqa"}
    assert matches_person_query("胡", **person)
    assert matches_person_query("hyq", **person)
    assert matches_person_query("huyu", **person)
    assert not matches_person_query("张", **person)
    assert matches_person_query("  ", **person)


def test_mixin_fills_columns_on_construct() -> None:
    person = _Person(name="胡玉琴A")
    assert person.name == "胡玉琴A"
    assert person.name_pinyin == "huyuqina"
    assert person.name_pinyin_initials == "hyqa"


def test_mixin_fills_columns_on_assignment() -> None:
    person = _Person(name="胡玉琴A")
    person.name = "张三"
    assert person.name_pinyin == "zhangsan"
    assert person.name_pinyin_initials == "zs"


def _open_session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    _Base.metadata.create_all(engine)
    return Session(engine)


def _matched_names(session: Session, keyword: str) -> set[str]:
    rows = session.scalars(
        select(_Person).where(
            person_name_match(keyword, _Person.name, _Person.name_pinyin, _Person.name_pinyin_initials)
        )
    ).all()
    return {row.name for row in rows}


def test_person_name_match_sql() -> None:
    with _open_session() as session:
        session.add_all([_Person(name="胡玉琴A"), _Person(name="其它人"), _Person(name="a_b")])
        session.commit()
        assert _matched_names(session, "hyq") == {"胡玉琴A"}
        assert _matched_names(session, "huyu") == {"胡玉琴A"}
        assert _matched_names(session, "Hu Yu") == {"胡玉琴A"}
        assert _matched_names(session, "胡") == {"胡玉琴A"}
        assert _matched_names(session, "hyq-") == set()
        assert _matched_names(session, "a_b") == {"a_b"}
        assert session.scalars(select(_Person).where(ilike_contains(_Person.name, "a_b"))).all()[0].name == "a_b"
        assert session.scalars(select(_Person).where(ilike_contains(_Person.name, "a%b"))).all() == []
