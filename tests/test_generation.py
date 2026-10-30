import ast
import importlib.util
import sys
from datetime import datetime

import pytest

from typed_psyco.generation import discover, format_sql, render
from typed_psyco.introspection import Column, Inspection, QueryInfo, placeholders


def rendered(source, queries, tmp_path, *, errors=None, old_models="", not_null=None):
    return render(
        source,
        discover(source),
        Inspection(queries, errors or {}, not_null or {}),
        models_module="models",
        old_models=old_models,
        directory=tmp_path,
    )


def load_models(source, tmp_path):
    path = tmp_path / "generated_models.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location("generated_models", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name)
    return module


@pytest.mark.parametrize(
    "import_line,constructor",
    [
        ("from typed_psyco import SQL", "SQL"),
        ("from typed_psyco.sql_subclass import SQL as Query", "Query"),
        ("import typed_psyco as tp", "tp.SQL"),
        ("import typed_psyco.sql_subclass as sql", "sql.SQL"),
        ("import typed_psyco.sql_subclass", "typed_psyco.sql_subclass.SQL"),
    ],
)
def test_discovery_imports(import_line, constructor):
    source = f"{import_line}\nq = {constructor}('select 1')\n"
    assert discover(source).definitions["q"].query == "select 1"


def test_static_discovery_does_not_execute_imports():
    source = (
        "import module_that_does_not_exist\n"
        "from models import MissingModel\n"
        "from typed_psyco import SQL\n"
        "raise RuntimeError('must never execute')\n"
        "q = SQL('select 1', MissingModel)\n"
    )
    assert discover(source).definitions["q"].query == "select 1"


@pytest.mark.parametrize(
    "value",
    [
        "SQL(query)",
        "SQL(f'{query}')",
        "SQL('select {}').format(x)",
        "SQL('select %s').join(parts)",
    ],
)
def test_unsupported_dynamic(value):
    result = discover(f"from typed_psyco import SQL\nq = {value}\n")
    assert not result.definitions
    assert "q" in result.errors


def test_unrelated_sql_is_not_discovered():
    assert not discover("from elsewhere import SQL\nq = SQL('select 1')").definitions


@pytest.mark.parametrize(
    "rebind",
    [
        "SQL = other",
        "from elsewhere import SQL",
        "def SQL(query): pass",
        "class SQL: pass",
    ],
)
def test_rebound_constructor_is_not_rewritten(rebind):
    source = f"from typed_psyco import SQL\n{rebind}\nq = SQL('select 1')\n"
    assert not discover(source).definitions


@pytest.mark.parametrize("second", ["SQL('select 2')", "other", "[SQL('select 2')]"])
def test_duplicate_assignments_are_reported(second):
    result = discover(
        f"from typed_psyco import SQL\nq = SQL('select 1')\nq = {second}\n"
    )
    assert not result.definitions
    assert "Multiple" in result.errors["q"]


def test_nested_expression_is_reported():
    result = discover("from typed_psyco import SQL\nq = [SQL('select 1')]\n")
    assert "q" in result.errors


@pytest.mark.parametrize("query", ["select %(named)s", "select %b", "select 5 % 2"])
def test_unsupported_placeholders(query):
    result = discover(f"from typed_psyco import SQL\nq = SQL({query!r})")
    assert "q" in result.errors


def test_placeholders():
    assert placeholders("select %s, 'a%%', %s")[0] == "select $1, 'a%', $2"


@pytest.mark.parametrize(
    "query",
    [
        "select 'quotes\\slash', '\"\"\"', $$ $1 $$, %s::text, '100%%'",
        "select '{literal}', 'line\nbreak', %s::int",
        "select '雪', %s::text",
        "select 'it''s literal', E'back\\\\slash', %s::int",
    ],
)
def test_formatting_preserves_literal_and_placeholder_values(query, tmp_path):
    formatted = format_sql(query, tmp_path)
    # SQLFluff's raw lexer exposes the actual string/placeholder tokens.
    from sqlfluff.core import Lexer

    def significant(text):
        parsed = Lexer(dialect="postgres").lex(text)
        assert parsed[0] is not None
        return [
            token.raw
            for token in parsed[0]
            if token.is_type("single_quote", "dollar_quote", "double_quote", "percent")
        ]

    assert significant(query) == significant(formatted)
    assert formatted.count("%s") == query.count("%s")
    assert "'100%%'" in formatted if "100%%" in query else True


def test_render_and_idempotence(tmp_path):
    source = (
        '"""Application queries."""\n'
        "from __future__ import annotations\n"
        "from typed_psyco import SQL\n"
        "# keep this comment\n"
        "q: SQL = SQL('select %s::timestamp as created', Old, tuple[str])\n"
        "unrelated = '雪'\n"
    )
    info = {"q": QueryInfo((datetime,), (Column("created", datetime),))}
    result = rendered(source, info, tmp_path)
    assert ast.get_docstring(ast.parse(result.source)) == "Application queries."
    assert "# keep this comment" in result.source
    assert "unrelated" in result.source
    assert "tuple[_typed_psyco_datetime.datetime]" in result.source
    assert result.successful == ("q",)
    second = rendered(result.source, info, tmp_path, old_models=result.models_source)
    assert result.source == second.source
    assert result.models_source == second.models_source
    model = load_models(result.models_source, tmp_path).QQuery
    row = model(created=None)
    assert row.unpack() == (None,)


def test_models_aliases_arrays_and_nullability(tmp_path):
    source = "from typed_psyco import SQL\nq = SQL('select 1')\n"
    columns = (
        Column("model_dump", int, 10, 1),
        Column("field_0", [int], 10, 2),
        Column("class", str),
        Column("unpack", bool),
        Column("_hidden", str),
    )
    result = rendered(
        source, {"q": QueryInfo((), columns)}, tmp_path, not_null={(10, 1): True}
    )
    model = load_models(result.models_source, tmp_path).QQuery
    row = model.model_validate(
        {"model_dump": 2, "field_0": [1], "class": None, "unpack": True, "_hidden": "a"}
    )
    assert row.unpack() == (2, [1], None, True, "a")
    assert "field_0_: int =" in result.models_source


def test_no_results_and_no_params(tmp_path):
    source = "from typed_psyco import SQL\nq = SQL('delete from t')\nr = SQL('delete from t where id = %s')\n"
    result = rendered(
        source, {"q": QueryInfo((), ()), "r": QueryInfo((int,), ())}, tmp_path
    )
    assert "None, tuple[int]" in result.source
    assert "from models import" not in result.source
    assert "class " not in result.models_source


def test_failed_models_and_definitions_are_preserved(tmp_path):
    source = (
        "from typed_psyco import SQL\n"
        "from models import BadQuery\n"
        "bad = SQL('select missing', BadQuery)\n"
        "good = SQL('select 1 as id')\n"
    )
    old_models = (
        "from pydantic import BaseModel\nclass BadQuery(BaseModel):\n    id: int\n"
    )
    result = rendered(
        source,
        {"good": QueryInfo((), (Column("id", int),))},
        tmp_path,
        errors={"bad": "missing relation"},
        old_models=old_models,
    )
    tree = ast.parse(result.source)
    bad = next(
        n for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id == "bad"
    )
    assert ast.literal_eval(bad.value.args[0]) == "select missing"
    assert "class BadQuery" in result.models_source
    assert "class GoodQuery" in result.models_source
    assert result.errors["bad"] == "missing relation"


def test_duplicate_columns_are_reported(tmp_path):
    result = rendered(
        "from typed_psyco import SQL\nq = SQL('select 1, 2')",
        {"q": QueryInfo((), (Column("id", int), Column("id", str)))},
        tmp_path,
    )
    assert not result.successful
    assert "Duplicate" in result.errors["q"]


def test_unicode_offsets(tmp_path):
    source = "from typed_psyco import SQL\n雪 = SQL('select 1')\n"
    result = rendered(source, {"雪": QueryInfo((), (Column("id", int),))}, tmp_path)
    assert "雪 = SQL(" in result.source
    ast.parse(result.source)


def test_local_sqlfluff_config_cannot_reorder_columns(tmp_path):
    (tmp_path / ".sqlfluff").write_text(
        "[sqlfluff]\ndialect = ansi\nrules = ST06\n"
        "[sqlfluff:rules:capitalisation.keywords]\ncapitalisation_policy = upper\n"
    )
    query = "select b + a, a, * from t"
    formatted = format_sql(query, tmp_path)
    assert formatted.upper().index("B + A") < formatted.upper().index("A,")
    assert formatted.index("*") > formatted.upper().index("A,")


def test_existing_rebound_import_does_not_break_parameter_types(tmp_path):
    source = (
        "import datetime\n"
        "datetime = 'not a module'\n"
        "from typed_psyco import SQL\n"
        "q = SQL('select %s::timestamp')\n"
    )
    result = rendered(source, {"q": QueryInfo((datetime,), ())}, tmp_path)
    assert "import datetime as _typed_psyco_datetime" in result.source
    assert "tuple[_typed_psyco_datetime.datetime]" in result.source


def test_dynamic_failed_model_is_not_overwritten_on_collision(tmp_path):
    source = (
        "from typed_psyco import SQL\n"
        "from models import FooBarQuery\n"
        "foo_bar = SQL(dynamic, FooBarQuery)\n"
        "foo__bar = SQL('select 1 as id')\n"
    )
    old_models = "from pydantic import BaseModel\nclass FooBarQuery(BaseModel):\n    previous: str\n"
    result = rendered(
        source,
        {"foo__bar": QueryInfo((), (Column("id", int),))},
        tmp_path,
        old_models=old_models,
    )
    models = load_models(result.models_source, tmp_path)
    assert set(models.FooBarQuery.model_fields) == {"previous"}
    assert set(models.FooBarQuery_2.model_fields) == {"id"}
    assert "foo_bar" in result.errors


def test_builtin_parameter_type_names_can_be_shadowed(tmp_path):
    source = (
        "from typed_psyco import SQL\n"
        "int = 'not a type'\n"
        "list = 'not a type'\n"
        "tuple = 'not a type'\n"
        "q = SQL('select %s::int[]')\n"
    )
    result = rendered(source, {"q": QueryInfo(([int],), ())}, tmp_path)
    namespace = {}
    exec(result.source, namespace)
    assert namespace["q"].as_string().lower() == "select %s::int[]"
    assert "_typed_psyco_builtins.tuple[" in result.source
    second = rendered(
        result.source,
        {"q": QueryInfo(([int],), ())},
        tmp_path,
        old_models=result.models_source,
    )
    assert second.source == result.source


def test_duplicate_result_failure_preserves_shared_model(tmp_path):
    source = (
        "from typed_psyco import SQL\n"
        "from models import SharedQuery\n"
        "good = SQL('select 1 as id', SharedQuery)\n"
        "bad = SQL('select 1, 2', SharedQuery)\n"
    )
    old_models = "from pydantic import BaseModel\nclass SharedQuery(BaseModel):\n    previous: str\n"
    result = rendered(
        source,
        {
            "good": QueryInfo((), (Column("id", int),)),
            "bad": QueryInfo((), (Column("id", int), Column("id", int))),
        },
        tmp_path,
        old_models=old_models,
    )
    models = load_models(result.models_source, tmp_path)
    assert set(models.SharedQuery.model_fields) == {"previous"}
    assert set(models.SharedQuery_2.model_fields) == {"id"}
    assert "bad" in result.errors


def test_model_fields_can_shadow_builtin_types(tmp_path):
    source = "from typed_psyco import SQL\nq = SQL('select 1 as int')\n"
    info = {"q": QueryInfo((), (Column("int", int), Column("list", [int])))}
    result = rendered(source, info, tmp_path)
    model = load_models(result.models_source, tmp_path).QQuery
    assert model(int=1, list=[2]).unpack() == (1, [2])
    second = rendered(result.source, info, tmp_path, old_models=result.models_source)
    assert second.models_source == result.models_source


def test_future_import_on_semicolon_line(tmp_path):
    source = '"""Docs"""; from __future__ import annotations; from typed_psyco import SQL\nq = SQL("select 1")\n'
    result = rendered(source, {"q": QueryInfo((), (Column("id", int),))}, tmp_path)
    assert ast.get_docstring(ast.parse(result.source)) == "Docs"
