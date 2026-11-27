import sqlite3

import pytest

from solution import build_search

ROWS = [
    (1, "Dune", "Frank Herbert", "111", 1965),
    (2, "100% Pure", "A. Writer", "222", 2001),
    (3, "snake_case", "B. Author", "333", 2010),
    (4, "O'Brien's Day", "C. Poet", "444", 1999),
    (5, "Plain", "D. Plain", "555", 1965),
    (6, "back\\slash", "E. Slash", "666", 2020),
]


@pytest.fixture
def db():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE books(id INTEGER PRIMARY KEY, title TEXT, author TEXT, isbn TEXT, year INTEGER)")
    con.executemany("INSERT INTO books VALUES (?,?,?,?,?)", ROWS)
    yield con
    con.close()


def search(db, column, term, **kw):
    sql, params = build_search(column, term, **kw)
    return [r[0] for r in db.execute(sql, params)]


def test_substring_match_is_case_insensitive(db):
    assert search(db, "title", "dune") == [1]
    assert search(db, "author", "plain") == [5]


def test_numeric_column(db):
    assert search(db, "year", "1965") == [1, 5]


def test_wildcards_are_literal(db):
    assert search(db, "title", "%") == [2]
    assert search(db, "title", "_") == [3]
    assert search(db, "title", "ak_") == []


def test_quote_is_data(db):
    assert search(db, "title", "O'Brien") == [4]
    assert search(db, "title", "x'; DROP TABLE books; --") == []
    assert db.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 6


def test_backslash_is_literal(db):
    assert search(db, "title", "\\") == [6]
    assert search(db, "title", "k\\s") == [6]
    assert search(db, "title", "\\%") == []


def test_rows_are_id_and_title(db):
    sql, params = build_search("title", "dune")
    assert db.execute(sql, params).fetchall() == [(1, "Dune")]
    sql, params = build_search("author", "slash")
    assert db.execute(sql, params).fetchall() == [(6, "back\\slash")]


def test_limit_bounds_are_inclusive(db):
    assert search(db, "year", "", limit=1) == [1]
    assert len(search(db, "year", "", limit=100)) == 6
    build_search("title", "x", limit=1)
    build_search("title", "x", limit=100)


def test_numeric_column_is_a_substring_match(db):
    assert search(db, "year", "965") == [1, 5]
    assert search(db, "year", "20") == [2, 3, 6]


def test_isbn_search(db):
    assert search(db, "isbn", "11") == [1]
    assert search(db, "isbn", "5") == [5]


def test_default_limit_is_20():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE books(id INTEGER PRIMARY KEY, title TEXT, author TEXT, isbn TEXT, year INTEGER)")
    con.executemany("INSERT INTO books VALUES (?,?,?,?,?)", [(i, "bulk %d" % i, "x", "0", 2000) for i in range(1, 31)])
    assert len(search(con, "title", "bulk")) == 20
    assert len(search(con, "title", "bulk", limit=30)) == 30
    assert search(con, "title", "bulk", limit=3) == [1, 2, 3]
    con.close()


@pytest.mark.parametrize("term", ["a\x00b", "\x00", None, 5, b"x"])
def test_invalid_terms_are_rejected(term):
    with pytest.raises(ValueError):
        build_search("title", term)


def test_limit_and_order(db):
    assert search(db, "year", "", limit=2) == [1, 2]
    assert search(db, "year", "") == [1, 2, 3, 4, 5, 6]


@pytest.mark.parametrize("column", ["id", "title; DROP TABLE books", "TITLE", "", "title OR 1=1"])
def test_column_allow_list(column):
    with pytest.raises(ValueError):
        build_search(column, "x")


@pytest.mark.parametrize("limit", [0, -1, 101, 1.5, "5", None, True])
def test_limit_validation(limit):
    with pytest.raises(ValueError):
        build_search("title", "x", limit=limit)


def test_values_are_not_in_the_sql():
    sql, params = build_search("title", "secret-needle")
    assert "secret-needle" not in sql
    assert sql.count("?") == len(params)
