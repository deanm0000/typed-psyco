# typed-psyco

A helper to psycopg 3 that type annotates SQL statements with Pydantic models from a live PostgreSQL database.

## Motivation

The goal is to keep SQL, parameter types, and result types together, so a type
checker can check how a query is called and how its results are used.

In FastAPI, it is convenient to inject a cursor into a route. Ideally, executing
a typed SQL object would be enough for the type checker to know the result type:

```python
@router.get("/")
async def root(cur: CUR):
    await cur.execute(sql_object)
    res = await cur.fetchall()
```

These are illustrative alternatives, not APIs provided by this library; `CUR`
and `CONN` stand for application-defined FastAPI dependency annotations.
Psycopg calls its cursor methods `fetchone()` and `fetchall()`. This library uses
`fetch_one()` and `fetch_all()` to distinguish its query helpers from those methods.

The problem is that a type checker cannot change the cursor's statically known
row type based on whichever SQL object was most recently executed. A custom
cursor subclass could instead make `execute()` return a cursor with the query's
result type:

```python
@router.get("/")
async def root(generic_cursor: CUR):
    cur = await generic_cursor.execute(sql_object)
    res = await cur.fetchall()
```

Python allows rebinding a variable to an object of a different type at runtime,
but a type checker would reject assigning the differently typed cursor back to
the dependency parameter with its declared type. The separate name `cur` lets
it infer the returned cursor's type instead.

Although psycopg's `execute()` already returns the cursor, I find this
assignment pattern awkward and unfamiliar; it would not be obvious to me why
capturing the return value is necessary. That is why I did not choose this
design. It would also still need a typed query object carrying the result and
parameter types, just as this library's `SQL` subclass does. Subclassing `SQL`
is not the only way to carry those types: a generic wrapper holding a psycopg
`SQL` object and its model could do that too.

Another option would be to generate only row factories and inject a connection:

```python
@router.get("/")
async def root(conn: CONN):
    async with conn.cursor(row_factory=this_qry_factory) as cur:
        await cur.execute(this_qry)
        res = await cur.fetchall()
```

Here, the programmer must keep `this_qry_factory` paired with `this_qry`. A type
checker can know what the factory returns without knowing whether the SQL
actually matches it. In my opinion, that largely defeats the purpose of typing
the query. Bundling the factory and SQL into a tuple would put them together,
but callers would still need to pass `this_qry[0]` and `this_qry[1]` separately,
and could change the query reference in one place without changing the other.

Instead, this library puts the fetch helpers on the typed SQL object. Callers
pass in their injected cursor, and the query object keeps the SQL and its
generated types paired.

### Incremental adoption

Subclassing psycopg's `SQL` also lets you adopt this library incrementally.
Generated model and parameter annotations are optional at runtime:

```python
from typed_psyco import SQL

myqry = SQL("select * from tbl")

async def example(cur):
    await cur.execute(myqry)
    return await cur.fetchall()
```

The unannotated query remains a psycopg SQL object and works as-is with existing
cursor execution and fetching. If you already have a file full of definitions
such as `qry1 = SQL(...)` and `qry2 = SQL(...)`, you can start by changing
`from psycopg.sql import SQL` to `from typed_psyco import SQL`, leaving those
definitions and their existing execution code unchanged.

Run `get_sql_types` at your convenience to add generated result models and
parameter types; it is not required before running the queries. Once generated,
the query helpers provide typed results without having to manage a separate
SQL object and row factory.

## Install and author queries

```sh
pip install typed-psyco
```

In your application's `db_sql.py`:

```python
from typed_psyco import SQL  # or: from typed_psyco.sql_subclass import SQL

get_user = SQL("SELECT id, name FROM users WHERE id = %s")
```

Generate against a database with the application's schema:

```sh
get_sql_types \
  --target-file ./db_sql.py \
  --models-file ./db_sql_models.py \
  --conn-str 'postgresql://localhost/my_database'
```

The generator creates `db_sql_models.py` and rewrites the query to include its
result model and positional parameter types:

```python
from db_sql_models import GetUserQuery
from typed_psyco import SQL

get_user = SQL("SELECT id, name FROM users WHERE id = %s", GetUserQuery, tuple[int])
```

SQL/Python formatting may change the layout. Generated models subclass Pydantic
`BaseModel` and include an `unpack()` method in the class definition, returning
their fields as a typed tuple in query-column order. Use SQL aliases for duplicate
result column names.

```python
async def example(cursor):
    user = await get_user.fetch_one(cursor, (42,))
    print(user.name)
    users = await get_user.fetch_all(cursor, (42,))
```

You can also unpack a result directly:

```python
id, name = (await get_user.fetch_one(cur, (42,))).unpack()
```

### When a query returns no rows

`fetch_one()` raises `LookupError` rather than returning `None`. This is an
opinionated choice: in my use cases, the overwhelming majority of calls expect
a result, and I would otherwise follow them with `assert res is not None`.
Raising keeps the successful return type as the generated model, without an
optional-result check before accessing fields or calling `unpack()`. Individual
fields can still be nullable.

If no result is an expected case, handle it explicitly with `try`/`except`:

```python
try:
    user = await get_user.fetch_one(cur, (42,))
except LookupError:
    print("No matching user")
else:
    print(user.name)
```

`fetch_all()` returns an empty list when there are no rows. Its return type
remains `list[GetUserQuery]` even when empty, so no optional-result check is
needed. To handle that case explicitly, check the list rather than catching a
no-result exception:

```python
users = await get_user.fetch_all(cur, (42,))
if not users:
    print("No matching users")
```

Both helpers validate tuple, dictionary, and namedtuple rows into the model,
and accept existing model instances. Typed constructor arguments let type
checkers infer result/parameter types. Generation is a development step, not a
runtime database requirement.

## Configuration

Each setting resolves independently: **CLI argument > environment variable**.
Missing required settings are errors; there are no application-specific defaults.
No `.env` file is loaded automatically.

| Argument          | Environment variable        | Required                         |
| ----------------- | --------------------------- | -------------------------------- |
| `--target-file`   | `TYPED_PSYCO_TARGET_FILE`   | Yes                              |
| `--models-file`   | `TYPED_PSYCO_MODELS_FILE`   | Yes                              |
| `--conn-str`      | `TYPED_PSYCO_CONN_STR`      | Yes                              |
| `--new-file`      | `TYPED_PSYCO_NEW_FILE`      | No                               |
| `--models-module` | `TYPED_PSYCO_MODELS_MODULE` | If import inference is ambiguous |
| `--[no-]sqlfluff` | `TYPED_PSYCO_SQLFLUFF`     | No; enabled if installed         |
| `--[no-]ruff`     | `TYPED_PSYCO_RUFF`         | No; enabled if installed         |

```sh
export TYPED_PSYCO_TARGET_FILE=./db_sql.py
export TYPED_PSYCO_MODELS_FILE=./db_sql_models.py
export TYPED_PSYCO_CONN_STR='postgresql://localhost/my_database'
python -m typed_psyco.get_sql_types
```

By default the target is rewritten in place. Set `--new-file ./db_sql_typed.py`
or `TYPED_PSYCO_NEW_FILE` to write a full annotated copy instead. The original
target is not modified; import the new module in your application to use its types.
The models file is written in either mode.

Paths are relative to the invocation's working directory; `~` is expanded.
Directories must already exist, and target/models/new-file paths must be distinct
(except the implicit in-place output). Empty required values and explicit empty
arguments are errors. Empty optional environment values mean unset.

Sibling modules and conventional packages containing `__init__.py` are inferred
automatically. For other layouts, such as a separate `src` tree, pass
`--models-module my_app.db_sql_models`. This is a Python import name, not a path;
ensure your application can import it. Moving an annotated copy does not relocate
its unrelated application imports.

Use environment variables instead of CLI connection strings to avoid placing
credentials in shell history or command-line process listings.

### Optional formatting

SQLFluff and Ruff are optional, not required dependencies. Each is used
independently when installed in the Python environment running `get_sql_types`;
generation still works if either or both are missing. To install both:

```sh
pip install 'typed-psyco[formatting]'
```

SQLFluff can pretty up your SQL statements using local SQLFluff configuration,
with PostgreSQL/raw templating and a restricted set of layout/capitalisation
rules. The column-reordering rule `ST06` (`structure.column_order`) is explicitly
disabled, even if enabled in local configuration: selected-column order must
stay aligned with the generated model and its `unpack()` tuple.

The generated Python is not necessarily formatted according to normal Python
formatting conventions, so, when available, Ruff formats both the rewritten
query module and generated models. It runs `ruff format --isolated` for
consistent formatting independent of local Ruff configuration; this formats
the code but does not promise to satisfy every lint rule in your project.

Disable either formatter, or both, even when installed:

```sh
get_sql_types --no-sqlfluff --no-ruff
```

This example assumes the required paths and connection string are set through
environment variables. Alternatively, set `TYPED_PSYCO_SQLFLUFF=false` and/or
`TYPED_PSYCO_RUFF=false`. Boolean environment settings accept `true`/`false`,
`1`/`0`, `yes`/`no`, and `on`/`off` (case-insensitive); empty means the default,
enabled if installed. `--sqlfluff` and `--ruff` override a disabling environment
setting, but do not require or install missing tools. Installed formatter
failures are reported as generation errors rather than silently ignored.

In the Python API, use `Config(..., sqlfluff=False, ruff=False)` to disable both.

## Python API and custom PostgreSQL types

```python
from pathlib import Path
from uuid import UUID

from typed_psyco.get_sql_types import Config, generate, write_result

config = Config(
    target_file=Path("db_sql.py"),
    models_file=Path("db_sql_models.py"),
    conn_str="postgresql://localhost/my_database",
)
result = generate(config, oid_types={123456: UUID})
for query, error in result.errors.items():
    print(query, error)
if result.successful_queries:
    write_result(config, result)
```

`generate` returns sources and per-query errors without writing files.
`write_result` publishes them. `Config.from_settings(...)` also supports the same
CLI/environment precedence, with an optional environment mapping for callers/tests.
Custom OID mappings override built-ins; use `[UUID]` for an array mapping.
Custom types must be importable module-level classes.

## Supported behavior and limitations

- Queries are discovered statically; the target and its imports are never executed.
  Supported definitions are module-level assignments or annotated assignments
  whose value is `SQL("literal", ...)`. Import aliases and qualified library imports
  are supported. Computed queries and `.format(...)` compositions are reported as
  unsupported. Queries inside functions/classes are outside the discovery scope.
- Only positional `%s` parameters and escaped `%%` are supported initially.
  Parameter types must be inferable by PostgreSQL; add SQL casts where necessary.
- PREPARE/describe discovers columns and parameters without executing the queries
  or DML. The database role needs schema/catalog access for those statements.
- Built-in OID mappings follow the source templates. Unknown OIDs emit a warning
  and fall back to `str`; override these for custom types. JSON is mapped to `dict`, and network
  types to IPv4 classes, as in the templates; override if your data needs otherwise.
- Expression columns are conservatively nullable. Catalog `NOT NULL` metadata
  cannot fully describe outer joins or other SQL semantics: review generated types.
- Statements with no result columns receive no result model. Async fetch helpers
  are for row-returning statements; execute no-result statements via psycopg.
- SQLFluff and Ruff are optional and independently configurable; see
  [Optional formatting](#optional-formatting). SQL formatting preserves selected-column
  order so generated models and `unpack()` remain aligned.
- Generation is repeatable. Existing model classes are retained, including stale
  classes, so failed/unsupported queries keep access to previously generated
  dependencies. No automatic pruning is performed.
- Partial query failures still write successful results, report diagnostics to
  stderr, and exit **1**. Failed definitions are not re-annotated. If no query
  succeeds, nothing is written. Configuration, connection, and rendering errors
  are fatal. Exit **0** means all discovered queries succeeded; argument errors
  exit **2**.
- Both files are staged before replacement, and each replacement is atomic.
  The pair is not a filesystem transaction; keep generated sources in version
  control. Avoid running multiple generators against the same output concurrently.

## Development

```sh
uv sync --group dev
uv run pytest
uv run ruff check .
uv run pyright
uv build
```

PostgreSQL integration tests are opt-in. Point `TYPED_PSYCO_TEST_CONN_STR` at a
**disposable test database**, not your application database. Tests create temporary
database objects and never load the reference application's queries.
