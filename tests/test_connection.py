import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg
import pytest
from psycopg import pq
from pydantic import BaseModel

from typed_psyco import SQL, AsyncConnection, Connection


class Row(BaseModel):
    id: int


def assert_model_factory(factory):
    result = SimpleNamespace(
        nfields=1, status=pq.ExecStatus.TUPLES_OK, fname=lambda index: b"id"
    )
    cur = SimpleNamespace(pgresult=result, _encoding="utf-8")
    assert factory(cur)(("42",)) == Row(id=42)


@pytest.mark.parametrize("with_model", [False, True])
@pytest.mark.parametrize(
    "options",
    [{}, {"prepare": True, "binary": True}, {"prepare": False, "binary": False}],
)
@pytest.mark.parametrize("params", [None, (42,), {"id": 42}])
def test_sync_executed_cursor(with_model, options, params):
    query = SQL("SELECT %s AS id", Row if with_model else None)
    cur = MagicMock()
    cur.__enter__.return_value = cur
    with patch.object(Connection, "cursor", return_value=cur) as make_cursor:
        conn = Connection(MagicMock())
        assert isinstance(conn, psycopg.Connection)
        with conn.exc_cur(query, params, **options) as actual:
            assert actual is cur
            cur.execute.assert_called_once_with(
                query,
                params,
                prepare=options.get("prepare"),
                binary=options.get("binary"),
            )
            cur.__exit__.assert_not_called()
        cur.__exit__.assert_called_once_with(None, None, None)
        if with_model:
            assert_model_factory(make_cursor.call_args.kwargs["row_factory"])
        else:
            make_cursor.assert_called_once_with()


@pytest.mark.parametrize("with_model", [False, True])
@pytest.mark.parametrize(
    "options",
    [{}, {"prepare": True, "binary": True}, {"prepare": False, "binary": False}],
)
@pytest.mark.parametrize("params", [None, (42,), {"id": 42}])
async def test_async_executed_cursor(with_model, options, params):
    query = SQL("SELECT %s AS id", Row if with_model else None)
    cur = MagicMock()
    cur.__aenter__.return_value = cur
    cur.execute = AsyncMock()
    with patch.object(AsyncConnection, "cursor", return_value=cur) as make_cursor:
        conn = AsyncConnection(MagicMock())
        assert isinstance(conn, psycopg.AsyncConnection)
        async with conn.exc_cur(query, params, **options) as actual:
            assert actual is cur
            cur.execute.assert_awaited_once_with(
                query,
                params,
                prepare=options.get("prepare"),
                binary=options.get("binary"),
            )
            cur.__aexit__.assert_not_awaited()
        cur.__aexit__.assert_awaited_once_with(None, None, None)
        if with_model:
            assert_model_factory(make_cursor.call_args.kwargs["row_factory"])
        else:
            make_cursor.assert_called_once_with()


@pytest.mark.parametrize("during_execute", [False, True])
def test_sync_closes_on_error(during_execute):
    error = ValueError("query or block failed")
    cur = MagicMock()
    cur.__exit__.return_value = False
    if during_execute:
        cur.execute.side_effect = error
    with patch.object(Connection, "cursor", return_value=cur):
        conn = Connection(MagicMock())
        with pytest.raises(ValueError) as caught:
            with conn.exc_cur(SQL("SELECT 1")):
                assert not during_execute
                raise error
        assert caught.value is error
        assert cur.__exit__.call_args.args[:2] == (ValueError, error)


@pytest.mark.parametrize("during_execute", [False, True])
@pytest.mark.parametrize("error_type", [ValueError, asyncio.CancelledError])
async def test_async_closes_on_error(during_execute, error_type):
    error = error_type("query or block failed")
    cur = MagicMock()
    cur.__aexit__.return_value = False
    cur.execute = AsyncMock(side_effect=error if during_execute else None)
    with patch.object(AsyncConnection, "cursor", return_value=cur):
        conn = AsyncConnection(MagicMock())
        with pytest.raises(error_type) as caught:
            async with conn.exc_cur(SQL("SELECT 1")):
                assert not during_execute
                raise error
        assert caught.value is error
        assert cur.__aexit__.await_args.args[:2] == (error_type, error)


def test_sync_default_parameters():
    cur = MagicMock()
    with patch.object(Connection, "cursor", return_value=cur):
        conn = Connection(MagicMock())
        query = SQL("SELECT 1")
        with conn.exc_cur(query):
            cur.execute.assert_called_once_with(query, None, prepare=None, binary=None)


async def test_async_default_parameters():
    cur = MagicMock()
    cur.execute = AsyncMock()
    with patch.object(AsyncConnection, "cursor", return_value=cur):
        conn = AsyncConnection(MagicMock())
        query = SQL("SELECT 1")
        async with conn.exc_cur(query):
            cur.execute.assert_awaited_once_with(query, None, prepare=None, binary=None)
