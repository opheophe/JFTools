from app.jellyfin_id import _extract_disc_year, clean_release_tags, name_match_score, year_match_category


def test_clean_release_tags_strips_noise():
    cleaned = clean_release_tags("45 2006 1080p AMZN WEBRip DDP5 1 x264 NTG ORARBG.mkv").lower()
    for token in ("1080p", "amzn", "webrip", "x264", "ntg", "orarbg", ".mkv"):
        assert token not in cleaned
    assert "45" in cleaned


def test_clean_release_tags_keeps_title_words():
    cleaned = clean_release_tags("Inception.2010.1080p.BluRay.x264-YIFY.mp4").lower()
    assert "inception" in cleaned
    assert "2010" in cleaned


def test_name_match_exact():
    assert name_match_score(clean_release_tags("The Matrix 1999"), "The Matrix", 1999) == 100


def test_name_match_mismatch_is_low():
    score = name_match_score(clean_release_tags("Totally Unrelated Film 2001"), "The Matrix", 1999)
    assert score < 50


def test_year_match_categories():
    assert year_match_category(1999, 1999) == "Correct"
    assert year_match_category(2000, 1999) == "+/-1 year"
    assert year_match_category(1998, 1999) == "+/-1 year"
    assert year_match_category(2005, 1999) == "Mismatch"
    assert year_match_category(None, 1999) == "No year"
    assert year_match_category(1999, None) == "No year"


def test_extract_disc_year_prefers_parenthesized():
    assert _extract_disc_year("/Filmer/2001 Maniacs (2005)/2001 Maniacs 2005 1080p BluRay.mp4") == 2005
    assert _extract_disc_year("/Filmer/2001 A Space Odyssey (1968)/2001 A Space Odyssey (1968) [BluRay].mp4") == 1968
    assert _extract_disc_year("/Filmer/1917 (2019)/1917 2019 PROPER 1080p BluRay.mp4") == 2019
