from app.jellyfin_id import clean_release_tags, match_score


def test_clean_release_tags_strips_noise():
    cleaned = clean_release_tags("45 2006 1080p AMZN WEBRip DDP5 1 x264 NTG ORARBG.mkv").lower()
    for token in ("1080p", "amzn", "webrip", "x264", "ntg", "orarbg", ".mkv"):
        assert token not in cleaned
    assert "45" in cleaned


def test_clean_release_tags_keeps_title_words():
    cleaned = clean_release_tags("Inception.2010.1080p.BluRay.x264-YIFY.mp4").lower()
    assert "inception" in cleaned
    assert "2010" in cleaned


def test_match_score_exact():
    assert match_score(clean_release_tags("The Matrix 1999"), "The Matrix", 1999) == 100


def test_match_score_mismatch_is_low():
    score = match_score(clean_release_tags("Totally Unrelated Film 2001"), "The Matrix", 1999)
    assert score < 50
