from collections import namedtuple
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from typed_psyco import SQL


class Row(BaseModel):
    id: int


def cursor(row):
    return SimpleNamespace(
        execute=AsyncMock(),
        fetchone=AsyncMock(return_value=row),
        fetchall=AsyncMock(return_value=[row]),
        description=[SimpleNamespace(name="id")],
    )


@pytest.mark.parametrize(
    "row", [(1,), {"id": 1}, namedtuple("Record", "id")(1), Row(id=1)]
)
async def test_row_shapes(row):
    cur = cursor(row)
    query = SQL("select id where id = %s", Row, tuple[int])
    assert await query.fetch_one(cur, (1,)) == Row(id=1)
    assert await query.fetch_all(cur, (1,)) == [Row(id=1)]
    cur.execute.assert_awaited_with(query, (1,))


async def test_empty_and_untyped_rows():
    cur = cursor(None)
    with pytest.raises(LookupError):
        await SQL("select 1", Row).fetch_one(cur)
    raw = cursor((1,))
    assert await SQL("select 1").fetch_one(raw) == (1,)
    assert await SQL("select 1").fetch_all(raw) == [(1,)]


async def test_invalid_shapes():
    for value in (object(), (1, 2)):
        with pytest.raises((TypeError, ValueError)):
            await SQL("select 1", Row).fetch_one(cursor(value))
    cur = cursor((1,))
    cur.description = None
    with pytest.raises(TypeError, match="description"):
        await SQL("select 1", Row).fetch_one(cur)
