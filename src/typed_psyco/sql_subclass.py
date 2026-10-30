from __future__ import annotations

from collections.abc import Sequence
from typing import (
    Any,
    Generic,
    LiteralString,
    Never,
    Protocol,
    TypeGuard,
    TypeVar,
    cast,
    overload,
)

from psycopg import AsyncCursor
from psycopg.abc import Params
from psycopg.sql import SQL as PsycopgSQL
from pydantic import BaseModel

NoParams = Sequence[Never] | None
_RowT = TypeVar("_RowT", bound=BaseModel)
_ParamsT = TypeVar("_ParamsT", bound=Params)


class NamedTupleish(Protocol):
    def _asdict(self) -> dict[str, object]: ...


def _is_namedtuple(obj: object) -> TypeGuard[NamedTupleish]:
    return isinstance(obj, tuple) and hasattr(obj, "_fields")


def _row_to_model(cur: AsyncCursor[Any], row: object, model_type: type[_RowT]) -> _RowT:
    if isinstance(row, model_type):
        return row
    if isinstance(row, dict):
        values = row
    elif _is_namedtuple(row):
        values = row._asdict()
    elif isinstance(row, tuple):
        if cur.description is None:
            raise TypeError("Cursor description is None, cannot map tuple to dict.")
        values = dict(zip((col.name for col in cur.description), row, strict=True))
    else:
        raise TypeError(f"Unexpected row type: {type(row)}")
    return model_type.model_validate(values)


class SQL(PsycopgSQL, Generic[_RowT, _ParamsT]):
    # Classic Generic is intentional: self-typed overloads bind both types
    # from constructor arguments, including Never for unannotated statements.
    @overload
    def __init__(
        self: SQL[_RowT, _ParamsT],  # pyright: ignore[reportInvalidTypeVarUse]
        query: LiteralString,
        model_type: type[_RowT],
        params: type[_ParamsT],
    ) -> None: ...

    @overload
    def __init__(
        self: SQL[_RowT, Never],  # pyright: ignore[reportInvalidTypeVarUse]
        query: LiteralString,
        model_type: type[_RowT],
    ) -> None: ...

    @overload
    def __init__(
        self: SQL[Never, _ParamsT],  # pyright: ignore[reportInvalidTypeVarUse]
        query: LiteralString,
        model_type: None,
        params: type[_ParamsT],
    ) -> None: ...

    @overload
    def __init__(
        self: SQL[Never, Never],
        query: LiteralString,
        model_type: None = None,
        params: None = None,
    ) -> None: ...

    def __init__(
        self,
        query: LiteralString,
        model_type: type[_RowT] | None = None,
        params: type[_ParamsT] | None = None,
    ) -> None:
        super().__init__(query)
        self.model_type = model_type

    @overload
    async def fetch_one(
        self: SQL[_RowT, _ParamsT], cur: AsyncCursor[Any], params: _ParamsT
    ) -> _RowT: ...

    @overload
    async def fetch_one(self: SQL[_RowT, Never], cur: AsyncCursor[Any]) -> _RowT: ...

    async def fetch_one(
        self, cur: AsyncCursor[Any], params: _ParamsT | None = None
    ) -> _RowT:
        await cur.execute(self, params)
        row = await cur.fetchone()
        if row is None:
            raise LookupError("Query returned no rows")
        if self.model_type is None:
            return cast("_RowT", row)
        return _row_to_model(cur, row, self.model_type)

    @overload
    async def fetch_all(
        self: SQL[_RowT, _ParamsT], cur: AsyncCursor[Any], params: _ParamsT
    ) -> list[_RowT]: ...

    @overload
    async def fetch_all(
        self: SQL[_RowT, Never], cur: AsyncCursor[Any]
    ) -> list[_RowT]: ...

    async def fetch_all(
        self, cur: AsyncCursor[Any], params: _ParamsT | None = None
    ) -> list[_RowT]:
        await cur.execute(self, params)
        rows = await cur.fetchall()
        if self.model_type is None:
            return cast("list[_RowT]", rows)
        return [_row_to_model(cur, row, self.model_type) for row in rows]
