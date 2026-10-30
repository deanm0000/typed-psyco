from __future__ import annotations

import keyword
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


def _module_name(value: str) -> bool:
    return all(p.isidentifier() and not keyword.iskeyword(p) for p in value.split("."))


def _package_root(path: Path) -> Path:
    directory = path.parent
    while (directory / "__init__.py").is_file():
        directory = directory.parent
    return directory


@dataclass(frozen=True)
class Config:
    target_file: Path
    models_file: Path
    conn_str: str = field(repr=False)
    new_file: Path | None = None
    models_module: str | None = None

    @property
    def output_file(self) -> Path:
        return self.new_file if self.new_file is not None else self.target_file

    def validated(self) -> Config:
        config = Config(
            target_file=self.target_file.expanduser().resolve(),
            models_file=self.models_file.expanduser().resolve(),
            conn_str=self.conn_str,
            new_file=self.new_file.expanduser().resolve() if self.new_file else None,
            models_module=self.models_module,
        )
        if not config.conn_str.strip():
            raise ValueError("conn-str must not be empty")
        if not config.target_file.is_file():
            raise ValueError(f"Target file does not exist: {config.target_file}")
        for path in (config.target_file, config.models_file, config.output_file):
            if path.suffix != ".py" or not _module_name(path.stem):
                raise ValueError(f"Expected an importable .py filename: {path}")
            if not path.parent.is_dir():
                raise ValueError(f"Output directory does not exist: {path.parent}")
            if path.exists() and not path.is_file():
                raise ValueError(f"Expected a regular file: {path}")
        if config.models_file in (config.target_file, config.output_file):
            raise ValueError("models-file must differ from target-file and new-file")
        if config.new_file == config.target_file:
            raise ValueError("new-file must differ from target-file")
        paths = (config.target_file, config.models_file, config.output_file)
        pairs = [(0, 1), (1, 2)]
        if config.new_file is not None:
            pairs.append((0, 2))
        for left, right in pairs:
            if paths[left].exists() and paths[right].exists():
                if paths[left].samefile(paths[right]):
                    raise ValueError("Input and output paths must not alias each other")
        config.import_module()
        return config

    def import_module(self) -> str:
        if self.models_module is not None:
            if not _module_name(self.models_module):
                raise ValueError("models-module must be a nonempty dotted module name")
            return self.models_module
        models_root = _package_root(self.models_file)
        output_root = _package_root(self.output_file)
        if models_root != output_root:
            raise ValueError(
                "Cannot infer models import; supply --models-module or "
                "TYPED_PSYCO_MODELS_MODULE for this layout"
            )
        parts = self.models_file.relative_to(models_root).with_suffix("").parts
        module = ".".join(parts)
        if not _module_name(module):
            raise ValueError("Cannot infer models import; supply --models-module")
        return module

    @classmethod
    def from_settings(
        cls,
        *,
        target_file: str | None = None,
        models_file: str | None = None,
        conn_str: str | None = None,
        new_file: str | None = None,
        models_module: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> Config:
        env = os.environ if environ is None else environ

        def setting(
            name: str, argument: str | None, required: bool = False
        ) -> str | None:
            value = argument if argument is not None else env.get(f"TYPED_PSYCO_{name}")
            if value is not None and not value.strip():
                if required or argument is not None:
                    raise ValueError(
                        f"{name.lower().replace('_', '-')} must not be empty"
                    )
                return None
            if required and value is None:
                raise ValueError(
                    f"Missing --{name.lower().replace('_', '-')} or TYPED_PSYCO_{name}"
                )
            return value

        target = setting("TARGET_FILE", target_file, True)
        models = setting("MODELS_FILE", models_file, True)
        connection = setting("CONN_STR", conn_str, True)
        output = setting("NEW_FILE", new_file)
        module = setting("MODELS_MODULE", models_module)
        assert target is not None and models is not None and connection is not None
        return cls(
            Path(target),
            Path(models),
            connection,
            Path(output) if output is not None else None,
            module,
        ).validated()
