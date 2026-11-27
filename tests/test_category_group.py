import pytest

from hari_lari_analytics.load import InputError, load_category_groups

from .conftest import FIXTURES


def test_loads_and_normalises_the_groups():
    groups = load_category_groups(FIXTURES / "category_group.csv")
    assert groups.set_index("category")["category_group_level_1"]["Mains"] == "food"
    assert len(groups) == 5


def test_missing_file_gives_an_empty_mapping(tmp_path):
    assert load_category_groups(tmp_path / "nope.csv").empty


def _write(tmp_path, body):
    path = tmp_path / "category_group.csv"
    path.write_text("category,category_group_level_1,category_group_level_2\n" + body)
    return path


def test_duplicate_category_is_an_error(tmp_path):
    with pytest.raises(InputError, match="more than once"):
        load_category_groups(_write(tmp_path, "Coffee,drink,\nCoffee,food,\n"))


def test_unknown_level_1_is_an_error(tmp_path):
    with pytest.raises(InputError, match="must be one of"):
        load_category_groups(_write(tmp_path, "Coffee,beverage,\n"))


def test_real_config_is_valid_and_covers_known_categories():
    groups = load_category_groups(FIXTURES.parents[1] / "config" / "category_group.csv")
    mapped = dict(zip(groups["category"], groups["category_group_level_1"], strict=True))
    for name in ["HYGO", "Casa Boa", "BAHASA Books"]:
        assert mapped[name] == "consignment"
