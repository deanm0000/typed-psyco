import json
import subprocess
import sys
from pathlib import Path


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
