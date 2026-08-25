"""The COPY encoder used to load a dogfood dataset into PostgreSQL.

These are pure-function tests: no server, no psycopg2. The encoder is where a
COPY loader goes wrong, and it goes wrong silently — an unescaped tab does not
raise, it shifts every column after it by one and lands a plausible dataset that
grades wrong. So the escaping rules get tested directly rather than inferred from
a green dogfood run.
"""

import sqlite3

import pytest

from scripts.dogfood.postgres_target import _CopyStream, _copy_encode, _copy_row

BACKSLASH = chr(92)


# ── Scalars ───────────────────────────────────────────────────────────────────

def test_none_becomes_the_null_marker():
    # \N is COPY text format's NULL. An empty string is a *value*, and the two
    # must not collapse into each other.
    assert _copy_encode(None) == BACKSLASH + "N"


def test_empty_string_is_not_null():
    assert _copy_encode("") == ""


def test_plain_text_passes_through():
    assert _copy_encode("Munich") == "Munich"


def test_integers_and_floats_render_as_text():
    assert _copy_encode(42) == "42"
    assert _copy_encode(3.5) == "3.5"


def test_bool_uses_postgres_literals():
    # Python's "True"/"False" are not accepted for a BOOLEAN column.
    assert _copy_encode(True) == "t"
    assert _copy_encode(False) == "f"


def test_bool_is_checked_before_int():
    # bool subclasses int; a naive isinstance(value, int) branch would emit "1".
    assert _copy_encode(True) != "1"


# ── The escapes that silently corrupt a load ──────────────────────────────────

def test_tab_is_escaped():
    # The column delimiter. Unescaped, it splits one value into two.
    assert _copy_encode("a\tb") == "a" + BACKSLASH + "tb"


def test_newline_is_escaped():
    # The row delimiter. Unescaped, it splits one row into two.
    assert _copy_encode("a\nb") == "a" + BACKSLASH + "nb"


def test_carriage_return_is_escaped():
    assert _copy_encode("a\rb") == "a" + BACKSLASH + "rb"


def test_backslash_is_escaped():
    assert _copy_encode("a" + BACKSLASH + "b") == "a" + BACKSLASH * 2 + "b"


def test_backslash_escaping_happens_first():
    # A literal backslash-n in the data must not be confused with a newline.
    # Escaping the newline before the backslash would give both inputs the same
    # encoding, and the round trip would silently invent a line break.
    literal = "a" + BACKSLASH + "nb"          # the two characters \ and n
    assert _copy_encode(literal) == "a" + BACKSLASH * 2 + "nb"
    assert _copy_encode("a\nb") == "a" + BACKSLASH + "nb"
    assert _copy_encode(literal) != _copy_encode("a\nb")


def test_literal_null_marker_in_text_survives():
    # The two characters \ and N in a text column are data, not a NULL.
    literal = BACKSLASH + "N"
    assert _copy_encode(literal) == BACKSLASH * 2 + "N"
    assert _copy_encode(literal) != _copy_encode(None)


# ── bytea ─────────────────────────────────────────────────────────────────────

def test_bytes_use_hex_format_with_an_escaped_backslash():
    # bytea input is \x…; inside COPY text the backslash itself is escaped.
    assert _copy_encode(b"ab") == BACKSLASH * 2 + "x6162"


def test_memoryview_is_treated_as_bytes():
    assert _copy_encode(memoryview(b"ab")) == BACKSLASH * 2 + "x6162"


# ── Rows ──────────────────────────────────────────────────────────────────────

def test_row_is_tab_separated_and_newline_terminated():
    assert _copy_row((1, "x", None)) == "1\tx\t" + BACKSLASH + "N\n"


def test_single_column_row_still_terminates():
    assert _copy_row(("only",)) == "only\n"


# ── The streaming adapter ─────────────────────────────────────────────────────

EXPECTED = (
    "1\ta\n"
    "2\t" + BACKSLASH + "N\n"
    "3\twith" + BACKSLASH + "ttab\n"
)


@pytest.fixture
def src(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    conn.executemany("INSERT INTO t VALUES (?, ?)",
                     [(1, "a"), (2, None), (3, "with\ttab")])
    conn.commit()
    return conn


def test_stream_yields_every_row(src):
    stream = _CopyStream(src.execute("SELECT id, name FROM t ORDER BY id"), batch=2)
    assert stream.read() == EXPECTED


def test_stream_reports_the_row_count(src):
    stream = _CopyStream(src.execute("SELECT id, name FROM t"), batch=2)
    stream.read()
    assert stream.rows == 3


def test_stream_respects_the_size_argument(src):
    # psycopg2 calls read(size); returning the whole table regardless would
    # defeat the point of streaming a 59M-row copy.
    stream = _CopyStream(src.execute("SELECT id, name FROM t ORDER BY id"), batch=1)
    first = stream.read(4)
    assert len(first) <= 4
    assert EXPECTED.startswith(first)


def test_stream_reassembles_across_reads(src):
    stream = _CopyStream(src.execute("SELECT id, name FROM t ORDER BY id"), batch=1)
    out = ""
    while True:
        chunk = stream.read(3)
        if not chunk:
            break
        out += chunk
    assert out == EXPECTED


def test_empty_table_streams_nothing(src):
    src.execute("DELETE FROM t")
    stream = _CopyStream(src.execute("SELECT id, name FROM t"), batch=2)
    assert stream.read() == ""
    assert stream.rows == 0


def test_readline_is_supported(src):
    # psycopg2's copy_from calls readline(); copy_expert calls read(). Support
    # both so the adapter cannot be wired up the wrong way round.
    stream = _CopyStream(src.execute("SELECT id, name FROM t ORDER BY id"), batch=2)
    assert stream.readline() == "1\ta\n"
    assert stream.readline() == "2\t" + BACKSLASH + "N\n"
