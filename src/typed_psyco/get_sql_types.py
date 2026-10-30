from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

import psycopg

from .config import Config
from .generation import discover, render
from .introspection import PythonType, inspect_queries

__all__ = ["Config", "GenerateResult", "generate", "write_result", "main"]


@dataclass(frozen=True)
class GenerateResult:
    sql_source: str
    models_source: str
    errors: dict[str, str]
    successful_queries: tuple[str, ...]


def generate(
    config: Config, *, oid_types: Mapping[int, PythonType] | None = None
) -> GenerateResult:
    """Describe queries and return generated sources without writing files."""
    config = config.validated()
    source = config.target_file.read_text(encoding="utf-8")
    discovery = discover(source)
    if not discovery.definitions:
        errors = discovery.errors or {"<module>": "No supported SQL definitions found"}
        return GenerateResult(source, "", errors, ())
    inspection = inspect_queries(
        config.conn_str,
        {name: definition.query for name, definition in discovery.definitions.items()},
        oid_types=oid_types,
    )
    if not inspection.queries:
        return GenerateResult(source, "", discovery.errors | inspection.errors, ())
    old_models = (
        config.models_file.read_text(encoding="utf-8")
        if config.models_file.exists()
        else ""
    )
    rendered = render(
        source,
        discovery,
        inspection,
        models_module=config.import_module(),
        old_models=old_models,
        directory=config.target_file.parent,
        use_sqlfluff=config.sqlfluff,
        use_ruff=config.ruff,
    )
    return GenerateResult(
        rendered.source, rendered.models_source, rendered.errors, rendered.successful
    )


def write_result(config: Config, result: GenerateResult) -> None:
    """Publish both staged sources; each replacement is atomic, not the pair."""
    config = config.validated()
    if not result.successful_queries:
        raise ValueError("No successful queries; refusing to replace output files")
    staged: list[tuple[Path, Path]] = []
    try:
        for path, source in (
            (config.models_file, result.models_source),
            (config.output_file, result.sql_source),
        ):
            compile(source, str(path), "exec")
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, delete=False
            ) as file:
                temporary = Path(file.name)
                staged.append((temporary, path))
                file.write(source)
                file.flush()
                os.fsync(file.fileno())
            if path.exists():
                temporary.chmod(path.stat().st_mode & 0o777)
        for temporary, path in staged:
            temporary.replace(path)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate Pydantic models and typed SQL statements from PostgreSQL"
    )
    for name in ("target-file", "models-file", "conn-str", "new-file", "models-module"):
        parser.add_argument(
            f"--{name}", help=f"Overrides TYPED_PSYCO_{name.upper().replace('-', '_')}"
        )
    for name in ("sqlfluff", "ruff"):
        parser.add_argument(
            f"--{name}",
            action=argparse.BooleanOptionalAction,
            default=None,
            help=(
                f"Use {name} if installed (default: enabled); "
                f"overrides TYPED_PSYCO_{name.upper()}"
            ),
        )
    args = parser.parse_args(argv)
    try:
        config = Config.from_settings(**vars(args))
    except ValueError as error:
        parser.error(str(error))
    try:
        result = generate(config)
        for name, error in result.errors.items():
            print(
                f"Error describing {name}: {_redact(error, config.conn_str)}",
                file=sys.stderr,
            )
        if result.successful_queries:
            write_result(config, result)
            print(f"Wrote {config.models_file} and {config.output_file}")
        return 1 if result.errors else 0
    except psycopg.OperationalError:
        # libpq messages may contain credentials or parts of the DSN.
        print(
            "Database connection/operation failed; check connectivity and credentials.",
            file=sys.stderr,
        )
        return 1
    except (
        OSError,
        ValueError,
        SyntaxError,
        psycopg.Error,
        subprocess.CalledProcessError,
    ) as error:
        print(
            f"Generation failed: {_redact(str(error), config.conn_str)}",
            file=sys.stderr,
        )
        return 1


def _redact(message: str, conn_str: str) -> str:
    from psycopg.conninfo import conninfo_to_dict

    message = message.replace(conn_str, "<connection string>")
    # Redact credential values even when a database/formatter message embeds
    # only a fragment rather than the complete connection string.
    try:
        options = conninfo_to_dict(conn_str)
    except psycopg.ProgrammingError:
        return "Invalid database connection settings"
    for name in ("password", "sslpassword"):
        secret = options.get(name)
        if isinstance(secret, str) and secret:
            message = message.replace(secret, "<redacted>")
    return message


if __name__ == "__main__":
    raise SystemExit(main())
