from __future__ import annotations

import ast
import keyword
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path

from pydantic import BaseModel

from .introspection import Inspection, PythonType, placeholders


@dataclass(frozen=True)
class SqlDef:
    name: str
    statement: ast.Assign | ast.AnnAssign
    call: ast.Call
    literal: ast.Constant
    query: str


@dataclass(frozen=True)
class Discovery:
    definitions: dict[str, SqlDef]
    errors: dict[str, str]


def discover(source: str) -> Discovery:
    tree = ast.parse(source)
    constructors: set[str] = set()
    modules: set[str] = set()
    definitions: dict[str, SqlDef] = {}
    errors: dict[str, str] = {}
    counts: Counter[str] = Counter()
    for statement in tree.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = (
                statement.targets
                if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            counts.update(
                node.id
                for target in targets
                for node in ast.walk(target)
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
            )

    def unbind(names: list[str]) -> None:
        constructors.difference_update(names)
        modules.difference_update(
            module for module in list(modules) if module.split(".")[0] in names
        )

    def sql_call(node: ast.AST) -> bool:
        return isinstance(node, ast.Call) and (
            ast.unparse(node.func) in constructors
            or any(ast.unparse(node.func) == f"{module}.SQL" for module in modules)
        )

    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom):
            unbind([alias.asname or alias.name for alias in statement.names])
            if statement.level == 0 and statement.module in (
                "typed_psyco",
                "typed_psyco.sql_subclass",
            ):
                constructors.update(
                    alias.asname or alias.name
                    for alias in statement.names
                    if alias.name == "SQL"
                )
        elif isinstance(statement, ast.Import):
            for alias in statement.names:
                unbind([alias.asname or alias.name.split(".")[0]])
                if alias.name in ("typed_psyco", "typed_psyco.sql_subclass"):
                    modules.add(alias.asname or alias.name)
        elif isinstance(
            statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            unbind([statement.name])
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        value = statement.value
        targets = (
            statement.targets
            if isinstance(statement, ast.Assign)
            else [statement.target]
        )
        names = [
            node.id
            for target in targets
            for node in ast.walk(target)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
        ]
        is_sql = sql_call(value) if value is not None else False
        # Also recognize SQL(...).format(...) as unsupported composition.
        has_sql = value is not None and any(sql_call(node) for node in ast.walk(value))
        unbind(names)
        if not has_sql:
            continue
        name = names[0] if names else f"line {statement.lineno}"
        if any(counts[n] > 1 for n in names):
            errors[name] = (
                "Multiple assignments to a SQL definition name are unsupported"
            )
            definitions.pop(name, None)
            continue
        if (
            not is_sql
            or not isinstance(value, ast.Call)
            or len(targets) != 1
            or not isinstance(targets[0], ast.Name)
            or not value.args
            or not isinstance(value.args[0], ast.Constant)
            or not isinstance(value.args[0].value, str)
        ):
            errors[name] = (
                "Only module-level name = SQL('literal', ...) definitions are supported"
            )
            continue
        literal = value.args[0]
        assert isinstance(literal.value, str)
        try:
            placeholders(literal.value)
        except ValueError as error:
            errors[name] = str(error)
            continue
        definitions[name] = SqlDef(name, statement, value, literal, literal.value)
    return Discovery(definitions, errors)


def position(source: str, node: ast.stmt | ast.expr, *, end: bool = False) -> int:
    lines = source.splitlines(keepends=True)
    lineno = node.end_lineno if end else node.lineno
    column = node.end_col_offset if end else node.col_offset
    assert lineno is not None and column is not None
    return sum(map(len, lines[: lineno - 1])) + len(
        lines[lineno - 1].encode("utf-8")[:column].decode("utf-8")
    )


def apply_edits(source: str, edits: list[tuple[int, int, str]]) -> str:
    for start, end, text in sorted(edits, reverse=True):
        source = source[:start] + text + source[end:]
    return source


def insert_imports(source: str, imports: list[str]) -> str:
    if not imports:
        return source
    tree = ast.parse(source)
    offset = 0
    for index, statement in enumerate(tree.body):
        if (
            index == 0
            and isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ) or (
            isinstance(statement, ast.ImportFrom) and statement.module == "__future__"
        ):
            offset = position(source, statement, end=True)
        else:
            if not offset:
                offset = position(source, statement)
            break
    text = "\n".join(imports) + "\n"
    if offset and source[offset - 1] != "\n":
        text = "\n" + text
    remainder = source[offset:]
    if remainder.startswith(";"):
        remainder = remainder[1:].lstrip(" \t")
    return source[:offset] + text + remainder


class TypeNames:
    def __init__(self, source: str) -> None:
        tree = ast.parse(source)
        self.used = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        } | {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.aliases: dict[str, str] = {}
        rebound = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
        }
        self.bound = rebound | {
            node.name
            for node in tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for statement in tree.body:
            if isinstance(statement, ast.Import):
                for alias in statement.names:
                    self.used.add(alias.asname or alias.name.split(".")[0])
                    self.bound.add(alias.asname or alias.name.split(".")[0])
                    if (
                        alias.asname
                        and alias.asname.startswith("_typed_psyco_")
                        and alias.asname not in rebound
                    ):
                        self.aliases[alias.name] = alias.asname
            elif isinstance(statement, ast.ImportFrom):
                self.used.update(a.asname or a.name for a in statement.names)
                self.bound.update(a.asname or a.name for a in statement.names)
        self.imports: list[str] = []

    def annotation(self, value: PythonType) -> str:
        if value is None:
            return "None"
        if isinstance(value, list):
            if len(value) != 1:
                raise ValueError("An array OID mapping must contain exactly one type")
            return f"{self.annotation(list)}[{self.annotation(value[0])}]"
        if value.__module__ == "builtins" and value.__name__ not in self.bound:
            return value.__qualname__
        if "<locals>" in value.__qualname__ or value.__module__ == "__main__":
            raise ValueError("Custom types must be importable module-level types")
        module = value.__module__
        if module not in self.aliases:
            alias = "_typed_psyco_" + module.replace(".", "_")
            while alias in self.used:
                alias += "_"
            self.used.add(alias)
            self.aliases[module] = alias
            self.imports.append(f"import {module} as {alias}")
        return f"{self.aliases[module]}.{value.__qualname__}"


def format_python(source: str) -> str:
    ast.parse(source)
    if find_spec("ruff") is None:
        return source
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "format",
            "--isolated",
            "--stdin-filename",
            "generated.py",
            "-",
        ],
        input=source,
        text=True,
        capture_output=True,
        check=True,
    )
    ast.parse(result.stdout)
    return result.stdout


def format_sql(query: str, directory: Path) -> str:
    if find_spec("sqlfluff") is None:
        return query
    import sqlfluff
    from sqlfluff.core import FluffConfig
    from sqlfluff.core.errors import SQLFluffUserError

    text, replacements = placeholders(query, formatting=True)
    try:
        config = FluffConfig.from_path(
            str(directory),
            overrides={
                "dialect": "postgres",
                "templater": "raw",
                "rules": "LT01,LT02,LT04,LT05,LT09,CP01,CP03",
                "exclude_rules": "ST06",
                "fix_even_unparsable": False,
            },
        )
        # ST06 reorders SELECT targets, breaking model and unpack() field order.
        fixed = sqlfluff.fix(text, config=config).strip("\n")
    except SQLFluffUserError as error:
        raise ValueError(f"SQLFluff formatting failed: {error}") from error
    if replacements:
        fixed = re.sub(
            r"\$\d+\b",
            lambda match: replacements.get(match.group(), match.group()),
            fixed,
        )
    return fixed


def _model_name(name: str) -> str:
    return (
        "".join(part[:1].upper() + part[1:] for part in name.split("_") if part)
        + "Query"
    )


def _render_model(
    name: str,
    info: Inspection,
    query_name: str,
    types: TypeNames,
) -> str:
    columns = info.queries[query_name].columns
    raw_names = [column.name for column in columns]
    reserved = set(raw_names)
    fields: list[str] = []
    annotations: list[str] = []
    body: list[str] = []
    for index, column in enumerate(columns):
        raw = column.name
        annotation = types.annotation(column.python_type)
        if annotation != "None" and not info.not_null.get(
            (column.table_oid, column.table_column), False
        ):
            annotation += " | None"
        valid = (
            raw.isidentifier()
            and not keyword.iskeyword(raw)
            and not raw.startswith(("_", "model_"))
            and not hasattr(BaseModel, raw)
            and raw != "unpack"
        )
        field = raw
        if not valid:
            field = f"field_{index}"
            while field in reserved:
                field += "_"
            reserved.add(field)
        fields.append(field)
        annotations.append(annotation)
        suffix = f" = Field(alias={raw!r})" if field != raw else ""
        body.append(f"    {field}: {annotation}{suffix}")
    values = ", ".join(f"self.{field}" for field in fields)
    if len(fields) == 1:
        values += ","
    return "\n".join(
        [
            f"class {name}(BaseModel):",
            "    model_config = ConfigDict(populate_by_name=True)",
            "",
            *body,
            "",
            f"    def unpack(self) -> tuple[{', '.join(annotations)}]:",
            f"        return ({values})",
        ]
    )


@dataclass(frozen=True)
class Rendered:
    source: str
    models_source: str
    successful: tuple[str, ...]
    errors: dict[str, str]


def render(
    source: str,
    discovery: Discovery,
    inspection: Inspection,
    *,
    models_module: str,
    old_models: str,
    directory: Path,
    use_sqlfluff: bool = True,
    use_ruff: bool = True,
) -> Rendered:
    errors = discovery.errors | inspection.errors
    queries = {}
    for name, info in inspection.queries.items():
        names = [col.name for col in info.columns]
        if len(set(names)) != len(names):
            errors[name] = "Duplicate result column names require explicit SQL aliases"
        else:
            queries[name] = info
    old_models = old_models or (
        "# Generated by typed_psyco; regenerate instead of editing.\n"
        "from __future__ import annotations\n\n"
    )
    old_tree = ast.parse(old_models)
    old_classes = {
        node.name: node for node in old_tree.body if isinstance(node, ast.ClassDef)
    }
    source_types = TypeNames(source)
    model_types = TypeNames(old_models)
    model_types.bound.update(
        col.name for info in queries.values() for col in info.columns
    )
    model_names: dict[str, str] = {}
    generated_imports: dict[str, str] = {}
    for statement in ast.parse(source).body:
        if isinstance(statement, ast.ImportFrom) and statement.module == models_module:
            generated_imports.update(
                (alias.asname or alias.name, alias.name) for alias in statement.names
            )
    failed_models: set[str] = set()
    for statement in ast.parse(source).body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            continue
        if any(
            statement.lineno == discovery.definitions[name].statement.lineno
            for name in queries
        ):
            continue
        failed_models.update(
            generated_imports[node.id]
            for node in ast.walk(statement)
            if isinstance(node, ast.Name) and node.id in generated_imports
        )
    reserved = source_types.used - set(generated_imports)
    # Every old model remains available for untouched/failed definitions.
    reserved |= failed_models
    edits: list[tuple[int, int, str]] = []
    model_edits: list[tuple[int, int, str]] = []
    append_models: list[str] = []
    successful: list[str] = []
    for name, info in queries.items():
        definition = discovery.definitions[name]
        model = "None"
        if info.columns:
            model = _model_name(name)
            previous = (
                definition.call.args[1] if len(definition.call.args) > 1 else None
            )
            if isinstance(previous, ast.Name) and previous.id in generated_imports:
                model = generated_imports[previous.id]
            base = model
            index = 2
            while model in reserved or model in model_names.values():
                model = f"{base}_{index}"
                index += 1
            try:
                block = _render_model(model, inspection, name, model_types)
            except ValueError as error:
                raise ValueError(
                    f"Cannot render model for query {name}: {error}"
                ) from error
            model_names[name] = model
            if model in old_classes:
                node = old_classes[model]
                model_edits.append(
                    (
                        position(old_models, node),
                        position(old_models, node, end=True),
                        block,
                    )
                )
            else:
                append_models.append(block)
        try:
            formatted = (
                format_sql(definition.query, directory)
                if use_sqlfluff
                else definition.query
            )
            params = [source_types.annotation(value) for value in info.parameters]
        except ValueError as error:
            errors[name] = str(error)
            # Rendering failures are fatal: don't publish a mismatched model/source pair.
            raise ValueError(f"Cannot render query {name}: {error}") from error
        args = f", {model}" if info.columns or params else ""
        if params:
            args += f", {source_types.annotation(tuple)}[{', '.join(params)}]"
        edits.append(
            (
                position(source, definition.literal),
                position(source, definition.call, end=True) - 1,
                repr(formatted) + args,
            )
        )
        if isinstance(definition.statement, ast.AnnAssign):
            target = definition.statement.target
            edits.append(
                (
                    position(source, target, end=True),
                    position(source, definition.call),
                    " = ",
                )
            )
        successful.append(name)
    rewritten = apply_edits(source, edits)
    if model_names:
        # Aliased old imports remain untouched; new imports bind canonical names.
        imports = [
            f"from {models_module} import {', '.join(sorted(model_names.values()))}"
        ]
    else:
        imports = []
    # Avoid adding the same import again on every generation.
    existing = {
        ast.unparse(node)
        for node in ast.parse(rewritten).body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    }
    imports = [line for line in imports + source_types.imports if line not in existing]
    rewritten = insert_imports(rewritten, imports)
    models_source = apply_edits(old_models, model_edits)
    if append_models:
        models_source = (
            models_source.rstrip() + "\n\n\n" + "\n\n\n".join(append_models) + "\n"
        )
    existing_models_imports = {
        ast.unparse(node)
        for node in ast.parse(models_source).body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    }
    model_imports = [
        "from pydantic import BaseModel, ConfigDict, Field"
    ] + model_types.imports
    models_source = insert_imports(
        models_source,
        [line for line in model_imports if line not in existing_models_imports],
    )
    return Rendered(
        format_python(rewritten) if use_ruff else rewritten,
        format_python(models_source) if use_ruff else models_source,
        tuple(successful),
        errors,
    )
