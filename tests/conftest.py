from pathlib import Path

import pytest

from typed_psyco.config import Config


@pytest.fixture
def config(tmp_path: Path) -> Config:
    target = tmp_path / "queries.py"
    target.write_text("from typed_psyco import SQL\nq = SQL('SELECT 1')\n")
    return Config(target, tmp_path / "models.py", "dbname=typed_psyco_test")
