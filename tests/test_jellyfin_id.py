import httpx

from app.jellyfin.client import JellyfinClient
from app.jellyfin_id import (
    _fetch_favorites,
    _favorited_by,
    clean_release_tags,
    name_match_score,
    year_match_category,
)


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


def test_name_match_title_is_a_release_token():
    # "Cam" is both a movie title and a release tag; it must not be stripped.
    cleaned = clean_release_tags("Cam (2018) [WEBRip] [1080p] [YTS] [YIFY].mp4")
    assert name_match_score(cleaned, "Cam", 2018) == 100


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


# ---- favorites --------------------------------------------------------------


def test_favorited_by_own_item():
    assert _favorited_by({"id": "m1", "series_id": ""}, {"m1": {"Alice", "Bob"}}) == ["Alice", "Bob"]


def test_favorited_by_includes_series_for_episode():
    # An episode counts as favorited when a user favorites it *or* its series.
    row = {"id": "e1", "series_id": "s1"}
    assert _favorited_by(row, {"e1": {"Alice"}, "s1": {"Bob", "Carol"}}) == ["Alice", "Bob", "Carol"]


def test_favorited_by_dedupes_same_user():
    row = {"id": "e1", "series_id": "s1"}
    assert _favorited_by(row, {"e1": {"Alice"}, "s1": {"Alice"}}) == ["Alice"]


def test_favorited_by_no_favorites():
    assert _favorited_by({"id": "x", "series_id": ""}, {}) == []


async def test_fetch_favorites_shapes_map_and_inverse():
    payloads = {
        "u1": [
            {"Id": "m1", "Type": "Movie", "Name": "Dune", "ProductionYear": 2021},
            {"Id": "s1", "Type": "Series", "Name": "Severance", "ProductionYear": 2022},
        ],
        "u2": [
            {"Id": "e1", "Type": "Episode", "Name": "Good News About Hell",
             "SeriesName": "Severance", "ProductionYear": 2022},
        ],
        "u3": [],
    }

    def handler(request):
        if request.url.path == "/Users":
            return httpx.Response(200, json=[
                {"Id": "u1", "Name": "Alice"},
                {"Id": "u2", "Name": "Bob"},
                {"Id": "u3", "Name": "Carol"},
            ])
        uid = request.url.path.split("/")[2]
        items = payloads[uid]
        return httpx.Response(200, json={"Items": items, "TotalRecordCount": len(items)})

    client = JellyfinClient("http://x:8096", "key", transport=httpx.MockTransport(handler))
    by_user, by_id = await _fetch_favorites(client)

    assert by_user["u1"][0]["display"] == "Dune (2021)"
    assert by_user["u1"][1]["display"] == "Severance (2022)"
    assert by_user["u2"][0]["display"] == "Severance — Good News About Hell"
    assert by_user["u3"] == []  # users with no favorites still get an (empty) entry
    assert by_id["m1"] == {"Alice"}
    assert by_id["s1"] == {"Alice"}
    assert by_id["e1"] == {"Bob"}
