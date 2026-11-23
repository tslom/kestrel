"""db.borrow(): the optional-caller-connection twin of db.session()."""

import sqlite3

import pytest

from app import db


def test_a_passed_connection_is_yielded_untouched_and_left_open(dbfile):
    with db.session() as mine:
        mine.execute("CREATE TEMP TABLE t (x)")
        mine.execute("BEGIN")
        mine.execute("INSERT INTO t VALUES (1)")
        with db.borrow(mine) as con:
            assert con is mine
        assert mine.in_transaction           # not committed
        mine.execute("SELECT 1")             # not closed
        mine.rollback()


def test_no_connection_opens_one_and_closes_it(dbfile):
    with db.borrow() as con:
        con.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        con.execute("SELECT 1")


def test_an_owned_connection_closes_even_on_error(dbfile):
    with pytest.raises(RuntimeError):
        with db.borrow(None) as con:
            raise RuntimeError
    with pytest.raises(sqlite3.ProgrammingError):
        con.execute("SELECT 1")


def test_a_passed_connection_survives_an_error(dbfile):
    with db.session() as mine:
        with pytest.raises(RuntimeError):
            with db.borrow(mine):
                raise RuntimeError
        mine.execute("SELECT 1")
