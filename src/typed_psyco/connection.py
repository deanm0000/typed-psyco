from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    asynccontextmanager,
    contextmanager,
)
from typing import Never, TypeVar, overload

from psycopg import AsyncConnection as PsycopgAsyncConnection
from psycopg import AsyncCursor, Cursor
from psycopg import Connection as PsycopgConnection
from psycopg.abc import Params
from psycopg.rows import class_row
from pydantic import BaseModel

from .sql_subclass import SQL

_RowT = TypeVar("_RowT", bound=BaseModel)
_ParamsT = TypeVar("_ParamsT", bound=Params)
_ConnectionRowT = TypeVar("_ConnectionRowT")


class Connection(PsycopgConnection[_ConnectionRowT]):
    """A psycopg connection with typed, executed cursor contexts."""

    # Never marks model-less SQL, whose rows use the connection's row type.
    @overload
    def exc_cur(  # pyright: ignore[reportOverlappingOverload]
        self,
        query: SQL[Never, _ParamsT],
        params: _ParamsT | None = None,
        *,
        prepare: bool | None = None,
        binary: bool | None = None,
    ) -> AbstractContextManager[Cursor[_ConnectionRowT]]: ...

    @overload
    def exc_cur(
        self,
        query: SQL[_RowT, _ParamsT],
        params: _ParamsT | None = None,
        *,
        prepare: bool | None = None,
        binary: bool | None = None,
    ) -> AbstractContextManager[Cursor[_RowT]]: ...

    @contextmanager
    def exc_cur(
        self,
        query: SQL[_RowT, _ParamsT],
        params: _ParamsT | None = None,
        *,
        prepare: bool | None = None,
        binary: bool | None = None,
    ) -> Generator[Cursor[_RowT] | Cursor[_ConnectionRowT], None, None]:
        """Execute on entry and close the cursor on exit, including on errors."""
        cur = (
            self.cursor(row_factory=class_row(query.model_type))
            if query.model_type is not None
            else self.cursor()
        )
        with cur:
            cur.execute(query, params, prepare=prepare, binary=binary)
            yield cur


class AsyncConnection(PsycopgAsyncConnection[_ConnectionRowT]):
    """An async psycopg connection with typed, executed cursor contexts."""

    # Never marks model-less SQL, whose rows use the connection's row type.
    @overload
    def exc_cur(  # pyright: ignore[reportOverlappingOverload]
        self,
        query: SQL[Never, _ParamsT],
        params: _ParamsT | None = None,
        *,
        prepare: bool | None = None,
        binary: bool | None = None,
    ) -> AbstractAsyncContextManager[AsyncCursor[_ConnectionRowT]]: ...

    @overload
    def exc_cur(
        self,
        query: SQL[_RowT, _ParamsT],
        params: _ParamsT | None = None,
        *,
        prepare: bool | None = None,
        binary: bool | None = None,
    ) -> AbstractAsyncContextManager[AsyncCursor[_RowT]]: ...

    @asynccontextmanager
    async def exc_cur(
        self,
        query: SQL[_RowT, _ParamsT],
        params: _ParamsT | None = None,
        *,
        prepare: bool | None = None,
        binary: bool | None = None,
    ) -> AsyncGenerator[AsyncCursor[_RowT] | AsyncCursor[_ConnectionRowT], None]:
        """Execute on entry and close the cursor on exit, including on errors."""
        cur = (
            self.cursor(row_factory=class_row(query.model_type))
            if query.model_type is not None
            else self.cursor()
        )
        async with cur:
            await cur.execute(query, params, prepare=prepare, binary=binary)
            yield cur
