from dataclasses import replace

import pytest

from typed_psyco.config import Config


def settings(config: Config) -> dict[str, str]:
    return {
        "target_file": str(config.target_file),
        "models_file": str(config.models_file),
        "conn_str": config.conn_str,
    }


@pytest.mark.parametrize("name", ["target_file", "models_file", "conn_str"])
@pytest.mark.parametrize("source", ["cli", "env"])
def test_required_precedence(config, name, source):
    kwargs = settings(config)
    env = {f"TYPED_PSYCO_{key.upper()}": value for key, value in kwargs.items()}
    if source == "env":
        del kwargs[name]
    else:
        env[f"TYPED_PSYCO_{name.upper()}"] = "invalid"
    actual = Config.from_settings(**kwargs, environ=env)
    assert actual == config.validated()


@pytest.mark.parametrize("name", ["target_file", "models_file", "conn_str"])
def test_missing_and_empty_required(config, name):
    kwargs = settings(config)
    del kwargs[name]
    with pytest.raises(ValueError, match="Missing"):
        Config.from_settings(**kwargs, environ={})
    kwargs[name] = ""
    with pytest.raises(ValueError, match="empty"):
        Config.from_settings(
            **kwargs, environ={f"TYPED_PSYCO_{name.upper()}": "fallback"}
        )


@pytest.mark.parametrize("name", ["new_file", "models_module"])
def test_optional_precedence(config, name):
    kwargs = settings(config)
    value = (
        str(config.target_file.parent / "typed.py")
        if name == "new_file"
        else "my.models"
    )
    env = {f"TYPED_PSYCO_{name.upper()}": value}
    actual = Config.from_settings(**kwargs, environ=env)
    assert str(getattr(actual, name)) == value
    kwargs[name] = value
    actual = Config.from_settings(
        **kwargs, environ={f"TYPED_PSYCO_{name.upper()}": "bad"}
    )
    assert str(getattr(actual, name)) == value
    kwargs[name] = ""
    with pytest.raises(ValueError, match="empty"):
        Config.from_settings(**kwargs, environ=env)
    del kwargs[name]
    assert (
        getattr(
            Config.from_settings(**kwargs, environ={f"TYPED_PSYCO_{name.upper()}": ""}),
            name,
        )
        is None
    )


def test_collisions_and_invalid_paths(config):
    for changed in (
        replace(config, models_file=config.target_file),
        replace(config, new_file=config.target_file),
        replace(config, new_file=config.models_file),
        replace(config, models_file=config.models_file.with_suffix(".txt")),
        replace(config, models_module="not-a.module"),
        replace(config, models_module="foo.class"),
        replace(config, models_file=config.models_file.parent / "absent" / "models.py"),
    ):
        with pytest.raises(ValueError):
            changed.validated()
    link = config.target_file.parent / "alias.py"
    link.hardlink_to(config.target_file)
    with pytest.raises(ValueError, match="alias"):
        replace(config, models_file=link).validated()


def test_import_inference(config, tmp_path):
    assert config.validated().import_module() == "models"
    package = tmp_path / "my_app"
    package.mkdir()
    (package / "__init__.py").touch()
    target = package / "queries.py"
    target.touch()
    packaged = replace(config, target_file=target, models_file=package / "models.py")
    assert packaged.validated().import_module() == "my_app.models"
    other = tmp_path / "src"
    other.mkdir()
    moved = replace(packaged, new_file=other / "typed.py")
    with pytest.raises(ValueError, match="Cannot infer"):
        moved.validated()
    assert (
        replace(moved, models_module="my_app.models").validated().import_module()
        == "my_app.models"
    )


def test_relative_and_home_paths(config, monkeypatch):
    monkeypatch.chdir(config.target_file.parent)
    monkeypatch.setenv("HOME", str(config.target_file.parent))
    actual = Config.from_settings(
        target_file="./queries.py",
        models_file="~/models.py",
        conn_str="dbname=test",
        environ={},
    )
    assert actual.target_file == config.target_file
    assert actual.models_file == config.models_file
    assert "dbname=test" not in repr(actual)
