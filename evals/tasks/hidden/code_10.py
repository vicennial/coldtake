import sqlite3

import pytest

from solution import find_users


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER)")
    c.executemany(
        "INSERT INTO users (name, age) VALUES (?, ?)",
        [("Alice", 30), ("alice", 17), ("Bob", 41), ("O'Brien", 52),
         ("Zed", 25), ("zed", 60), ("ZED", 40),
         ("Neg", -5), ("\u00c9mile", 30), ("\u00e9mile", 31), ("q' OR 'a'='a", 33)],
    )
    c.commit()
    yield c
    c.close()


def test_case_insensitive_match_and_order(conn):
    assert find_users(conn, "ALICE") == [(1, "Alice", 30), (2, "alice", 17)]


def test_min_age(conn):
    assert find_users(conn, "alice", 18) == [(1, "Alice", 30)]


def test_min_age_is_inclusive(conn):
    assert find_users(conn, "alice", 17) == [(1, "Alice", 30), (2, "alice", 17)]
    assert find_users(conn, "Alice", 30) == [(1, "Alice", 30)]
    assert find_users(conn, "Alice", 31) == []


def test_results_are_ordered_by_id_not_by_age(conn):
    assert find_users(conn, "zed") == [(5, "Zed", 25), (6, "zed", 60), (7, "ZED", 40)]


def test_negative_ages_and_the_default_cutoff(conn):
    assert find_users(conn, "neg") == []
    assert find_users(conn, "neg", -10) == [(8, "Neg", -5)]
    assert find_users(conn, "neg", -5) == [(8, "Neg", -5)]
    assert find_users(conn, "neg", -4) == []


def test_nocase_folds_ascii_only(conn):
    assert find_users(conn, "\u00c9MILE") == [(9, "\u00c9mile", 30)]      # ASCII letters fold
    assert find_users(conn, "\u00c9mile") == [(9, "\u00c9mile", 30)]      # the accented letter does not
    assert find_users(conn, "\u00e9mile") == [(10, "\u00e9mile", 31)]


def test_a_stored_name_that_looks_like_sql_is_found_literally(conn):
    assert find_users(conn, "q' OR 'a'='a") == [(11, "q' OR 'a'='a", 33)]


def test_wildcards_are_not_special(conn):
    assert find_users(conn, "Al%") == []
    assert find_users(conn, "_ob") == []
    assert find_users(conn, "%") == []


def test_injection_payload_matches_nothing(conn):
    assert find_users(conn, "x' OR '1'='1") == []
    assert find_users(conn, "' OR 1=1 --") == []


def test_apostrophe_in_legitimate_name(conn):
    assert find_users(conn, "O'Brien") == [(4, "O'Brien", 52)]


def test_stacked_statement_payload_is_inert(conn):
    find_users(conn, "x'; DROP TABLE users; --")
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 11
