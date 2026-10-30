import json
import subprocess
import sys
from pathlib import Path


def check_typing(sample: Path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pyright",
            "--outputjson",
            "--pythonversion",
            "3.11",
            str(sample),
        ],
        capture_output=True,
        text=True,
    )
    report = json.loads(result.stdout)
    diagnostics = report["generalDiagnostics"]
    expected = {
        index
        for index, line in enumerate(sample.read_text().splitlines())
        if "# expect error" in line
    }
    assert {d["range"]["start"]["line"] for d in diagnostics} == expected, report
    assert all(d["severity"] == "error" for d in diagnostics)


def test_constructor_and_fetch_typing(tmp_path: Path):
    sample = tmp_path / "typing_sample.py"
    sample.write_text(
        """
from typing import Any, assert_type
from psycopg import AsyncCursor
from pydantic import BaseModel
from typed_psyco import SQL

class Row(BaseModel):
    id: int

query = SQL("select id where id = %s", Row, tuple[int])
plain = SQL("select id", Row)

async def use(cur: AsyncCursor[Any]) -> None:
    assert_type(await query.fetch_one(cur, (1,)), Row)
    assert_type(await query.fetch_all(cur, (1,)), list[Row])
    assert_type(await plain.fetch_one(cur), Row)
    await query.fetch_one(cur, ("bad",))  # expect error
    await query.fetch_one(cur)  # expect error

bad: SQL[Row, tuple[int]] = SQL("select 1")  # expect error
"""
    )
    check_typing(sample)


def test_connection_cursor_typing(tmp_path: Path):
    sample = tmp_path / "connection_typing_sample.py"
    sample.write_text(
        """
from typing import Any, TypedDict, assert_type
from psycopg import AsyncCursor, Cursor
from pydantic import BaseModel
from typed_psyco import SQL, AsyncConnection, Connection

class Row(BaseModel):
    id: int

class NamedParams(TypedDict):
    id: int

query = SQL("SELECT %s AS id", Row, tuple[int])
named = SQL("SELECT %(id)s AS id", Row, NamedParams)
plain = SQL("SELECT 1 AS id", Row)
raw = SQL("SELECT 1")
raw_params = SQL("SELECT %s", None, tuple[int])

def sync_use(conn: Connection[tuple[Any, ...]], custom: Connection[dict[str, Any]]) -> None:
    with conn.exc_cur(query, (1,), prepare=True, binary=False) as cur:
        assert_type(cur, Cursor[Row])
        assert_type(cur.fetchall(), list[Row])
        assert_type(cur.fetchone(), Row | None)
    with conn.exc_cur(named, {"id": 1}) as named_cur:
        assert_type(named_cur, Cursor[Row])
    with conn.exc_cur(plain) as plain_cur:
        assert_type(plain_cur, Cursor[Row])
    with conn.exc_cur(raw) as raw_cur:
        assert_type(raw_cur, Cursor[tuple[Any, ...]])
    with custom.exc_cur(raw) as custom_cur:
        assert_type(custom_cur, Cursor[dict[str, Any]])
    with conn.exc_cur(raw_params, (1,)) as params_cur:
        assert_type(params_cur, Cursor[tuple[Any, ...]])
    conn.exc_cur(query)
    conn.exc_cur(query, None)
    conn.exc_cur(query, ("bad",))  # expect error
    conn.exc_cur(named, {"id": "bad"})  # expect error
    conn.exc_cur(plain, (1,))  # expect error
    conn.exc_cur(raw_params, ("bad",))  # expect error
    conn.exc_cur("SELECT 1")  # expect error
    conn.exc_cur(query, (1,), True)  # expect error
    assert_type(Connection.connect(""), Connection[Any])

async def async_use(conn: AsyncConnection[tuple[Any, ...]], custom: AsyncConnection[dict[str, Any]]) -> None:
    async with conn.exc_cur(query, (1,), prepare=False, binary=True) as cur:
        assert_type(cur, AsyncCursor[Row])
        assert_type(await cur.fetchall(), list[Row])
        assert_type(await cur.fetchone(), Row | None)
    async with conn.exc_cur(named, {"id": 1}) as named_cur:
        assert_type(named_cur, AsyncCursor[Row])
    async with conn.exc_cur(plain) as plain_cur:
        assert_type(plain_cur, AsyncCursor[Row])
    async with conn.exc_cur(raw) as raw_cur:
        assert_type(raw_cur, AsyncCursor[tuple[Any, ...]])
    async with custom.exc_cur(raw) as custom_cur:
        assert_type(custom_cur, AsyncCursor[dict[str, Any]])
    async with conn.exc_cur(raw_params, (1,)) as params_cur:
        assert_type(params_cur, AsyncCursor[tuple[Any, ...]])
    conn.exc_cur(query)
    conn.exc_cur(query, None)
    conn.exc_cur(query, ("bad",))  # expect error
    conn.exc_cur(named, {"id": "bad"})  # expect error
    conn.exc_cur(plain, (1,))  # expect error
    conn.exc_cur(raw_params, ("bad",))  # expect error
    conn.exc_cur("SELECT 1")  # expect error
    conn.exc_cur(query, (1,), True)  # expect error
    assert_type(await AsyncConnection.connect(""), AsyncConnection[Any])
"""
    )
    check_typing(sample)
