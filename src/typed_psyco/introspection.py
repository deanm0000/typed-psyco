from __future__ import annotations

import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from ipaddress import IPv4Address, IPv4Network
from typing import TypeAlias
from uuid import UUID

import psycopg
from psycopg import pq
from psycopg.errors import error_from_result
from psycopg.sql import SQL, Identifier

PythonType: TypeAlias = type | list["PythonType"] | None

POSTGRES_OID_TO_PYTHON_TYPE: dict[int, PythonType] = {
    16: bool,
    20: int,
    21: int,
    23: int,
    700: float,
    701: float,
    1700: Decimal,
    18: str,
    19: str,
    25: str,
    1042: str,
    1043: str,
    114: dict,
    3802: dict,
    142: str,
    17: bytes,
    1082: date,
    1083: time,
    1114: datetime,
    1184: datetime,
    1186: timedelta,
    2950: UUID,
    869: IPv4Address,
    650: IPv4Network,
    2278: None,
    1000: [bool],
    1005: [int],
    1007: [int],
    1016: [int],
    1021: [float],
    1022: [float],
    1009: [str],
    1015: [str],
    1115: [datetime],
    1182: [date],
    1185: [datetime],
    1231: [Decimal],
    2951: [UUID],
}


@dataclass(frozen=True)
class Column:
    name: str
    python_type: PythonType
    table_oid: int = 0
    table_column: int = 0


@dataclass(frozen=True)
class QueryInfo:
    parameters: tuple[PythonType, ...]
    columns: tuple[Column, ...]


@dataclass(frozen=True)
class Inspection:
    queries: dict[str, QueryInfo]
    errors: dict[str, str]
    not_null: dict[tuple[int, int], bool]


def placeholders(query: str, *, formatting: bool = False) -> tuple[str, dict[str, str]]:
    """Translate psycopg's positional placeholders, not PostgreSQL string tokens."""
    pieces: list[str] = []
    replacements: dict[str, str] = {}
    position = 0
    number = 1
    while position < len(query):
        char = query[position]
        if char != "%":
            pieces.append(char)
            position += 1
            continue
        pair = query[position : position + 2]
        if pair == "%%":
            pieces.append("%%" if formatting else "%")
        elif pair == "%s":
            if formatting:
                while f"${number}" in query:
                    number += 1
            marker = f"${number}"
            pieces.append(marker)
            replacements[marker] = "%s"
            number += 1
        else:
            raise ValueError(
                "Only positional %s and escaped %% placeholders are supported"
            )
        position += 2
    return "".join(pieces), replacements


def inspect_queries(
    conn_str: str,
    queries: Mapping[str, str],
    *,
    oid_types: Mapping[int, PythonType] | None = None,
) -> Inspection:
    mapping = POSTGRES_OID_TO_PYTHON_TYPE | dict(oid_types or {})

    def python_type(oid: int) -> PythonType:
        if oid not in mapping:
            warnings.warn(
                f"Unknown PostgreSQL OID {oid}; falling back to str. "
                "Supply an oid_types override for this type.",
                UserWarning,
                stacklevel=2,
            )
            mapping[oid] = str
        return mapping[oid]

    results: dict[str, QueryInfo] = {}
    errors: dict[str, str] = {}
    # Autocommit prevents a failed PREPARE from poisoning subsequent statements.
    with psycopg.connect(conn_str, autocommit=True) as conn:
        encoding = conn.info.encoding
        statement_name = "_typed_psyco_describe"
        for name, query in queries.items():
            prepared = False
            try:
                text, replacements = placeholders(query)
                # Protocol-level prepare cannot execute trailing statements.
                result = conn.pgconn.prepare(
                    statement_name.encode(encoding), text.encode(encoding)
                )
                if result.status != pq.ExecStatus.COMMAND_OK:
                    raise error_from_result(result, encoding)
                prepared = True
                result = conn.pgconn.describe_prepared(statement_name.encode(encoding))
                if result.status != pq.ExecStatus.COMMAND_OK:
                    raise error_from_result(result, encoding)
                if result.nparams != len(replacements):
                    raise ValueError(
                        "Parameter count mismatch: use unquoted %s placeholders, "
                        "not native $n parameters or placeholders inside SQL literals"
                    )
                parameters = tuple(
                    python_type(result.param_type(i)) for i in range(result.nparams)
                )
                columns: list[Column] = []
                for i in range(result.nfields):
                    raw_name = result.fname(i)
                    if raw_name is None:
                        raise RuntimeError("Describe returned a column without a name")
                    columns.append(
                        Column(
                            raw_name.decode(encoding),
                            python_type(result.ftype(i)),
                            result.ftable(i),
                            result.ftablecol(i),
                        )
                    )
                results[name] = QueryInfo(parameters, tuple(columns))
            except psycopg.Error as error:
                if isinstance(
                    error, (psycopg.OperationalError, psycopg.InterfaceError)
                ):
                    raise
                errors[name] = str(error)
            except ValueError as error:
                errors[name] = str(error)
            finally:
                if prepared:
                    conn.execute(
                        SQL("DEALLOCATE {}").format(Identifier(statement_name))
                    )
        keys = sorted(
            {
                (col.table_oid, col.table_column)
                for info in results.values()
                for col in info.columns
                if col.table_oid and col.table_column > 0
            }
        )
        not_null: dict[tuple[int, int], bool] = {}
        if keys:
            rows = conn.execute(
                "SELECT attrelid, attnum, attnotnull FROM pg_attribute "
                "WHERE (attrelid, attnum) IN "
                "(SELECT * FROM unnest(%s::oid[], %s::int2[]))",
                ([key[0] for key in keys], [key[1] for key in keys]),
            ).fetchall()
            not_null = {(row[0], row[1]): row[2] for row in rows}
    return Inspection(results, errors, not_null)
