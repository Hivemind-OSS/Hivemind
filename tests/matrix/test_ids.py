"""C1 contract: node-ID minting — direct recipe tests."""

from __future__ import annotations

from hive.matrix import ids


def test_normalize_idempotent():
    for s in [
        "Foo.Bar",
        "a__b",
        "__x__",
        "café",
        "Ünïcödé",
        "a b c",
        "数据",
        "",
        ".._..",
    ]:
        once = ids.normalize_id(s)
        assert ids.normalize_id(once) == once


def test_normalize_recipe_examples():
    assert ids.normalize_id("Foo.Bar") == "foo_bar"
    assert ids.normalize_id("__leading_trailing__") == "leading_trailing"
    assert ids.normalize_id("a   b") == "a_b"
    assert ids.normalize_id("CamelCase") == "camelcase"
    assert ids.normalize_id("a---b...c") == "a_b_c"


def test_make_id_joins_and_drops_empties():
    assert ids.make_id("dir", "file", "sym") == ids.normalize_id("dir_file_sym")
    assert ids.make_id("", "x", "") == "x"
    assert ids.make_id("a.", "_b_") == "a_b"


def test_casefold_applied():
    assert ids.normalize_id("ABC") == "abc"
    # locale-tricky casefold must not crash
    assert ids.normalize_id("İstanbul")


def test_unicode_letters_survive():
    out = ids.normalize_id("数据_func")
    assert "数" in out and "据" in out
    assert ids.normalize_id("Привет") == "привет".casefold()
