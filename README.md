# typed-psyco

A helper to psycopg 3 that type annotates SQL statements with Pydantic models automatically from a live PostgreSQL database.

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
`BaseModel` and include an `unpack()` method returning their fields as a typed tuple.
Use SQL aliases for duplicate result column names.

```python
async def example(cursor):
    user = await get_user.fetch_one(cursor, (42,))
    print(user.name)
    users = await get_user.fetch_all(cursor, (42,))
```

`fetch_one` raises `LookupError` when no row is returned. Both helpers validate
tuple, dictionary, and namedtuple rows into the model, and accept existing model
instances. Typed constructor arguments let type checkers infer result/parameter
types. Generation is a development step, not a runtime database requirement.

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
- SQLFluff uses PostgreSQL/raw templating, local configuration, and only layout/
  capitalisation rules. Ruff formats the generated Python independently of local
  formatting configuration. Both tools are declared dependencies.
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
