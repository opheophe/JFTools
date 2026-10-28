"""The generated ISO 639 table and its lookup rules."""

import pytest

from app.languages import (
    language_code,
    language_family,
    language_name,
    language_prefix_name,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("fil", "Filipino"),
        ("tgl", "Tagalog"),
        ("nb", "Norwegian Bokmål"),
        ("nob", "Norwegian Bokmål"),
        ("nn", "Norwegian Nynorsk"),
        ("nno", "Norwegian Nynorsk"),
        ("no", "Norwegian"),
        ("nor", "Norwegian"),
        ("ger", "German"),
        ("deu", "German"),
        ("fre", "French"),
        ("fra", "French"),
        ("el", "Greek"),
        ("ell", "Greek"),
        ("und", "Unknown"),
        ("zzzz", "Unknown"),
    ],
)
def test_precise_language_names(value, expected):
    assert language_name(value) == expected


def test_names_regions_and_common_aliases():
    assert language_name("English") == "English"
    assert language_name("en-US") == "English"
    assert language_name("pt_BR") == "Portuguese"
    assert language_name("Català") == "Catalan"
    assert language_name("catala") == "Catalan"
    assert language_name("Bokmal") == "Norwegian Bokmål"
    assert language_name("Norsk Bokmål") == "Norwegian Bokmål"


def test_preferred_codes_prefer_iso_639_1():
    assert language_code("English") == "en"
    assert language_code("German") == "de"
    assert language_code("Filipino") == "fil"
    assert language_code("Norwegian Bokmål") == "nb"
    assert language_code("Tagalog") == "tl"
    assert language_code("Unknown") == ""


def test_families_keep_preferences_broad():
    assert language_family("Norwegian") == "Norwegian"
    assert language_family("Norwegian Bokmål") == "Norwegian"
    assert language_family("Norwegian Nynorsk") == "Norwegian"
    assert language_family("Filipino") == "Tagalog"
    assert language_family("Tagalog") == "Tagalog"
    assert language_family("Mandarin Chinese") == "Chinese"
    assert language_family("Cantonese") == "Chinese"
    assert language_family("Unknown") == "Unknown"


def test_prefix_matching_ignores_bare_codes():
    assert language_prefix_name("english") == "English"
    assert language_prefix_name("catalan") == "Catalan"
    # "mov" is a valid ISO 639-3 code (Mohave), but not a name prefix.
    assert language_prefix_name("mov") == "Unknown"
    assert language_prefix_name("en") == "Unknown"
