from dataclasses import replace
from unittest.mock import patch

import pytest

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
