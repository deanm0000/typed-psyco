import importlib.util
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.sql import SQL, Identifier

from typed_psyco.get_sql_types import generate, write_result
from typed_psyco.introspection import inspect_queries

pytestmark = pytest.mark.postgres


@pytest.fixture
def database():
    conn_str = os.environ.get("TYPED_PSYCO_TEST_CONN_STR")
    if not conn_str:
        pytest.skip("Set TYPED_PSYCO_TEST_CONN_STR to a disposable database")
    schema = "typed_psyco_test_" + uuid4().hex
    with psycopg.connect(conn_str, autocommit=True) as conn:
        conn.execute(SQL("CREATE SCHEMA {}").format(Identifier(schema)))
        conn.execute(
            SQL(
                "CREATE TABLE {}.items (id int NOT NULL, label text, tags text[])"
            ).format(Identifier(schema))
        )
        try:
            yield conn_str, schema
        finally:
            conn.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(schema)))


def test_describe_and_cleanup(database):
    conn_str, schema = database
    inspection = inspect_queries(
        conn_str,
        {
            "select": f"SELECT * FROM {schema}.items WHERE id = %s",
            "bad": f"SELECT * FROM {schema}.missing",
            "delete": f"DELETE FROM {schema}.items WHERE id = %s",
            "escaped": "SELECT 'a%%b'::text as value, %s::uuid as uuid",
            "insert": f"INSERT INTO {schema}.items VALUES (1, 'must not execute', NULL)",
            "multiple": f"SELECT 1; INSERT INTO {schema}.items VALUES (2, 'never', NULL)",
            "quoted_parameter": "SELECT '%s'::text",
            "native_parameter": "SELECT $1::int",
        },
    )
    assert inspection.queries["select"].parameters == (int,)
    columns = inspection.queries["select"].columns
    assert [col.name for col in columns] == ["id", "label", "tags"]
    assert columns[2].python_type == [str]
    assert inspection.not_null[(columns[0].table_oid, columns[0].table_column)]
    assert "bad" in inspection.errors
    assert "multiple" in inspection.errors
    assert "quoted_parameter" in inspection.errors
    assert "native_parameter" in inspection.errors
    assert inspection.queries["delete"].columns == ()
    with psycopg.connect(conn_str) as conn:
        assert conn.execute(f"SELECT count(*) FROM {schema}.items").fetchone()[0] == 0
    override = inspect_queries(conn_str, {"q": "select 1::int"}, oid_types={23: str})
    assert override.queries["q"].columns[0].python_type is str


def test_describe_many_queries(database):
    conn_str, _ = database
    queries = {f"query_{i}": "SELECT %s::int AS id" for i in range(40)}
    result = inspect_queries(conn_str, queries)
    assert not result.errors
    assert len(result.queries) == 40


def test_unknown_type_warns_and_can_be_overridden(database):
    conn_str, schema = database
    with psycopg.connect(conn_str, autocommit=True) as conn:
        conn.execute(f"CREATE TYPE {schema}.mood AS ENUM ('happy')")
        row = conn.execute("SELECT %s::regtype::oid", (f"{schema}.mood",)).fetchone()
        assert row is not None
        oid = row[0]
    query = {"q": f"SELECT NULL::{schema}.mood AS mood"}
    with pytest.warns(UserWarning, match=f"Unknown PostgreSQL OID {oid}"):
        result = inspect_queries(conn_str, query)
    assert result.queries["q"].columns[0].python_type is str
    assert (
        inspect_queries(conn_str, query, oid_types={oid: int})
        .queries["q"]
        .columns[0]
        .python_type
        is int
    )


def test_end_to_end_and_repeat(database, config, monkeypatch):
    conn_str, schema = database
    config = replace(config, conn_str=conn_str)
    config.target_file.write_text(
        f"from typed_psyco import SQL\nq = SQL('SELECT * FROM {schema}.items WHERE id = %s')\n"
    )
    first = generate(config)
    assert not first.errors
    write_result(config, first)
    second = generate(config)
    assert not second.errors
    assert first.sql_source == second.sql_source
    assert first.models_source == second.models_source
    monkeypatch.syspath_prepend(str(config.target_file.parent))
    sys.modules.pop("models", None)
    spec = importlib.util.spec_from_file_location(
        "integration_queries", config.output_file
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        assert module.q.model_type(id=1, label=None, tags=[]).unpack() == (1, None, [])
    finally:
        sys.modules.pop("models", None)


def test_installed_cli_env_and_new_file(database, config):
    conn_str, schema = database
    original = (
        f"from typed_psyco import SQL\nq = SQL('SELECT id FROM {schema}.items')\n"
    )
    config.target_file.write_text(original)
    output = config.target_file.with_name("queries_typed.py")
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("TYPED_PSYCO_")
    }
    env.update(
        TYPED_PSYCO_TARGET_FILE=str(config.target_file),
        TYPED_PSYCO_MODELS_FILE=str(config.models_file),
        TYPED_PSYCO_CONN_STR=conn_str,
        TYPED_PSYCO_NEW_FILE=str(output),
    )
    result = subprocess.run(
        [str(Path(sys.executable).parent / "get_sql_types")],
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert config.target_file.read_text() == original
    assert output.exists()
    result = subprocess.run(
        [sys.executable, "-m", "typed_psyco.get_sql_types"],
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


async def test_live_async_fetch(database, config, monkeypatch):
    conn_str, schema = database
    config = replace(config, conn_str=conn_str)
    config.target_file.write_text(
        f"from typed_psyco import SQL\nq = SQL('SELECT id, label FROM {schema}.items WHERE id = %s')\n"
    )
    result = generate(config)
    write_result(config, result)
    monkeypatch.syspath_prepend(str(config.target_file.parent))
    sys.modules.pop("models", None)
    spec = importlib.util.spec_from_file_location("live_queries", config.output_file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        async with await psycopg.AsyncConnection.connect(conn_str) as conn:
            await conn.execute(f"INSERT INTO {schema}.items VALUES (1, 'hello', NULL)")
            async with conn.cursor() as cur:
                row = await module.q.fetch_one(cur, (1,))
                assert row.id == 1 and row.label == "hello"
                assert (await module.q.fetch_all(cur, (1,)))[0] == row
    finally:
        sys.modules.pop("models", None)
