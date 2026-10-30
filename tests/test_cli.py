from dataclasses import replace
from unittest.mock import patch

import pytest

from typed_psyco.generation import format_python, format_sql
from typed_psyco.get_sql_types import GenerateResult, generate, main, write_result
from typed_psyco.introspection import Column, Inspection, QueryInfo


def arguments(config):
    return [
        "--target-file",
        str(config.target_file),
        "--models-file",
        str(config.models_file),
        "--conn-str",
        config.conn_str,
    ]


@pytest.mark.parametrize("new_file", [False, True])
def test_generate_and_write_without_importing_target(config, new_file):
    source = (
        "from typed_psyco import SQL\n"
        "import nonexistent_application\n"
        "q = SQL('select 1 as id')\n"
    )
    config.target_file.write_text(source)
    if new_file:
        config = replace(config, new_file=config.target_file.with_name("typed.py"))
    info = Inspection({"q": QueryInfo((), (Column("id", int),))}, {}, {})
    with patch("typed_psyco.get_sql_types.inspect_queries", return_value=info):
        result = generate(config)
    assert config.target_file.read_text() == source
    assert not config.models_file.exists()
    write_result(config, result)
    assert "class QQuery" in config.models_file.read_text()
    assert "QQuery" in config.output_file.read_text()
    if new_file:
        assert config.target_file.read_text() == source


def test_partial_cli_failure(config, capsys):
    result = GenerateResult("q = 1\n", "# models\n", {"bad": "missing table"}, ("q",))
    with patch("typed_psyco.get_sql_types.generate", return_value=result):
        assert main(arguments(config)) == 1
    assert "bad" in capsys.readouterr().err
    assert config.target_file.read_text() == "q = 1\n"


def test_no_success_does_not_write(config):
    original = config.target_file.read_bytes()
    result = GenerateResult("", "", {"q": "failed"}, ())
    with patch("typed_psyco.get_sql_types.generate", return_value=result):
        assert main(arguments(config)) == 1
    assert config.target_file.read_bytes() == original
    assert not config.models_file.exists()
    with pytest.raises(ValueError, match="No successful"):
        write_result(config, result)


def test_missing_config_is_argument_error(monkeypatch, capsys):
    for name in ("TARGET_FILE", "MODELS_FILE", "CONN_STR"):
        monkeypatch.delenv(f"TYPED_PSYCO_{name}", raising=False)
    with pytest.raises(SystemExit) as error:
        main([])
    assert error.value.code == 2
    assert "TYPED_PSYCO_TARGET_FILE" in capsys.readouterr().err


def test_staging_failure_leaves_both_files(config):
    original = config.target_file.read_bytes()
    result = GenerateResult("invalid @", "# models\n", {}, ("q",))
    with pytest.raises(SyntaxError):
        write_result(config, result)
    assert not config.models_file.exists()
    assert config.target_file.read_bytes() == original
    assert sorted(p.name for p in config.target_file.parent.iterdir()) == ["queries.py"]


def test_secrets_are_redacted(config, capsys):
    config = replace(config, conn_str="dbname=test password=unique_secret")
    result = GenerateResult("", "", {"q": "unique_secret " + config.conn_str}, ())
    with patch("typed_psyco.get_sql_types.generate", return_value=result):
        main(arguments(config))
    output = capsys.readouterr()
    assert "unique_secret" not in output.err


@pytest.mark.parametrize("name", ["sqlfluff", "ruff"])
@pytest.mark.parametrize("enabled", [False, True])
def test_formatter_cli_overrides_environment(config, monkeypatch, name, enabled):
    monkeypatch.setenv(f"TYPED_PSYCO_{name.upper()}", str(not enabled))
    result = GenerateResult("", "", {"q": "failed"}, ())
    flag = f"--{name}" if enabled else f"--no-{name}"
    with patch("typed_psyco.get_sql_types.generate", return_value=result) as mocked:
        assert main(arguments(config) + [flag]) == 1
    assert getattr(mocked.call_args.args[0], name) is enabled


@pytest.mark.parametrize("use_sqlfluff", [False, True])
@pytest.mark.parametrize("use_ruff", [False, True])
def test_generate_formatter_controls(config, use_sqlfluff, use_ruff):
    config = replace(config, sqlfluff=use_sqlfluff, ruff=use_ruff)
    query = "select  1 as id"
    config.target_file.write_text(f"from typed_psyco import SQL\nq = SQL({query!r})\n")
    info = Inspection({"q": QueryInfo((), (Column("id", int),))}, {}, {})
    with (
        patch("typed_psyco.get_sql_types.inspect_queries", return_value=info),
        patch("typed_psyco.generation.format_sql", wraps=format_sql) as sql_formatter,
        patch(
            "typed_psyco.generation.format_python", wraps=format_python
        ) as python_formatter,
    ):
        result = generate(config)
    assert sql_formatter.call_count == int(use_sqlfluff)
    assert python_formatter.call_count == 2 * int(use_ruff)
    if not use_sqlfluff:
        assert query in result.sql_source
    write_result(config, result)


def test_sqlfluff_errors_are_reported(config, capsys):
    from sqlfluff.core.errors import SQLFluffUserError

    config.target_file.write_text("from typed_psyco import SQL\nq = SQL('select 1')\n")
    original = config.target_file.read_text()
    info = Inspection({"q": QueryInfo((), (Column("id", int),))}, {}, {})
    with (
        patch("typed_psyco.get_sql_types.inspect_queries", return_value=info),
        patch("sqlfluff.fix", side_effect=SQLFluffUserError("bad formatter config")),
    ):
        assert main(arguments(config)) == 1
    assert "SQLFluff formatting failed: bad formatter config" in capsys.readouterr().err
    assert config.target_file.read_text() == original
    assert not config.models_file.exists()
