from app.jellyfin_id import clean_release_tags, name_match_score, year_match_category


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


def test_year_match_correct():
    # exact year present in the path
    assert year_match_category("/Filmer/The Matrix (1999)/The Matrix 1999 1080p.mkv", 1999) == "Correct"
    # title number ("2001") must not confuse the match
    assert year_match_category("/Filmer/2001 Maniacs (2005)/2001 Maniacs 2005 1080p BluRay.mp4", 2005) == "Correct"
    assert year_match_category("/Filmer/2001 A Space Odyssey (1968)/2001 A Space Odyssey (1968) [BluRay].mp4", 1968) == "Correct"
    assert year_match_category("/Filmer/1917 (2019)/1917 2019 PROPER 1080p BluRay.mp4", 2019) == "Correct"
    # mislabeled folder: the filename year wins
    assert year_match_category("/Filmer/Extinction (2020)/Extinction.2018.1080p.NF.WEB-DL.DDP5.1.x264-NTG.mkv", 2018) == "Correct"
    # release-group tag with embedded year ("EDGE2020") is ignored
    assert year_match_category("/Filmer/101 Dalmatians (1996)/101 Dalmatians 1996 1080p AMZN WEBRip DD+ 2.0 x265-EDGE2020.mkv", 1996) == "Correct"


def test_year_match_off_by_one():
    assert year_match_category("/Filmer/Movie/Movie.1994.1080p.mkv", 1995) == "+/-1 year"
    assert year_match_category("/Filmer/Movie/Movie.1996.1080p.mkv", 1995) == "+/-1 year"


def test_year_match_mismatch():
    assert year_match_category("/Filmer/Movie (2005)/Movie.2005.1080p.mkv", 2010) == "Mismatch"


def test_year_match_no_year():
    assert year_match_category("/Filmer/Movie/Movie.1080p.mkv", 2010) == "No year"
    assert year_match_category("/Filmer/Movie (2010)/Movie.1080p.mkv", None) == "No year"
