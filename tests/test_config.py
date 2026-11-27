"""The private business config (plan t11): a local folder or gs://, every seed file required,
and `config/examples/` with the same columns as the real files."""

from pathlib import Path

import pandas as pd
import pytest

from hari_lari_analytics.core.config import ConfigError, config_location, local_config
from hari_lari_analytics.core.store import GcsStore
from hari_lari_analytics.warehouse.db import SEED_FILES, Seeds, open_run

from .conftest import FIXTURES
from .fakes import GcsClient

EXAMPLES = Path("config/examples")
REAL = Path("config")
REQUIRED = [file for file, _ in SEED_FILES.values()]


def test_the_location_is_the_option_else_the_variable_else_config(monkeypatch):
    monkeypatch.delenv("CONFIG_DIR", raising=False)
    assert config_location() == "config"
    monkeypatch.setenv("CONFIG_DIR", "gs://bucket/config")
    assert config_location() == "gs://bucket/config"
    assert config_location("elsewhere") == "elsewhere"


def test_a_gcs_location_is_copied_to_a_temporary_folder():
    client = GcsClient()
    store = GcsStore("bucket", "config", client)
    for name in REQUIRED:
        store.upload(name, FIXTURES / name)
    store.write_text("old/item_aliases.csv", "not read: only the top level")
    with local_config("gs://bucket/config", REQUIRED, store) as folder:
        assert sorted(p.name for p in folder.iterdir()) == sorted(REQUIRED)
        seeds = Seeds.from_dir(folder)
    assert not folder.exists()
    assert len(seeds.item_aliases) == 2


def test_a_missing_required_file_fails_and_names_the_location(tmp_path):
    (tmp_path / "item_aliases.csv").write_text("sales_item,adjusted_item,adjusted_category\n")
    with pytest.raises(ConfigError, match=rf"{tmp_path} has no category_group.csv, calendar"):
        Seeds.load(tmp_path)


def test_a_missing_file_on_gcs_fails_too():
    store = GcsStore("bucket", "config", GcsClient())
    with pytest.raises(ConfigError, match="gs://bucket/config has no item_aliases.csv"):
        with local_config("gs://bucket/config", REQUIRED, store):
            pass


def test_the_examples_load_and_build_the_models(run_dir):
    seeds = Seeds.load(EXAMPLES)
    assert not seeds.item_aliases.empty and not seeds.category_groups.empty
    db = open_run(run_dir, EXAMPLES)
    assert db.con.execute("select count(*) from fct_sales_lines").fetchone()[0] > 0


@pytest.mark.parametrize("name", REQUIRED)
def test_the_examples_have_the_columns_of_the_real_files(name):
    if not (REAL / name).exists():
        pytest.skip("the real config is not here (it is private)")
    real = pd.read_csv(REAL / name, nrows=0, encoding="utf-8-sig").columns.tolist()
    example = pd.read_csv(EXAMPLES / name, nrows=0).columns.tolist()
    assert example == real
