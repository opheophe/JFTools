import asyncio
import json

import httpx

import app.jellyfin_id as jid
from app.jellyfin.client import JellyfinClient
from app.jellyfin_id import (
    _fetch_favorites,
    _fetch_items,
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


def test_name_match_roman_sequel_not_capped():
    # "Part II" in the file vs "2" in the title must not trip the digit cap.
    disc = clean_release_tags("Class of Nuke 'Em High Part II - Subhumanoid Meltdown 1991 1080p web YTS.mp4")
    assert name_match_score(disc, "Class of Nuke 'Em High 2: Subhumanoid Meltdown", 1991) >= 60


def test_name_match_roman_and_arabic_are_equal():
    assert name_match_score(clean_release_tags("Rocky III 1976"), "Rocky 3", 1976) == 100
    assert name_match_score(clean_release_tags("Rocky 2 1979"), "Rocky II", 1979) == 100
    assert name_match_score(clean_release_tags("Friday the 13th Part II 1981"), "Friday the 13th Part 2", 1981) == 100


def test_name_match_single_letters_not_roman():
    # Single-letter Roman numerals must not be rewritten into digits.
    assert name_match_score(clean_release_tags("X-Men 2000"), "X-Men", 2000) == 100
    assert name_match_score(clean_release_tags("XMen 2000"), "X-Men", 2000) >= 30
    assert name_match_score(clean_release_tags("I Am Legend 2007"), "I Am Legend", 2007) == 100
    assert name_match_score(clean_release_tags("IAmLegend 2007"), "I Am Legend", 2007) >= 20


def test_name_match_single_letter_roman_avoids_false_cap():
    # "V" is Roman for 5: a Jellyfin digit must accept the numeral on the file side.
    assert name_match_score(clean_release_tags("Saw V 2008"), "Saw 5", 2008) >= 40


def test_name_match_missing_sequel_number_still_capped():
    # A genuinely different installment (no number in the file) still caps low.
    assert name_match_score(clean_release_tags("Rocky 1976"), "Rocky 5", 1976) <= 20


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


# ---- fetching ---------------------------------------------------------------


async def test_fetch_items_skips_series_rows():
    """Series are show folders, not media: even if Jellyfin returns one, it
    must not reach the stored rows or the scan worklist."""
    seen = {}

    def handler(request):
        if request.url.path == "/Library/VirtualFolders":
            return httpx.Response(200, json=[{"Name": "Shows", "Locations": ["/Serier"]}])
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={
            "Items": [
                {"Id": "s1", "Type": "Series", "Name": "Severance",
                 "Path": "/Serier/Severance", "ProductionYear": 2022},
                {"Id": "m1", "Type": "Movie", "Name": "Dune",
                 "Path": "/Filmer/Dune/Dune.mkv", "ProductionYear": 2021},
                {"Id": "e1", "Type": "Episode", "Name": "Good News About Hell",
                 "SeriesName": "Severance", "SeriesId": "s1",
                 "Path": "/Serier/Severance/Season 1/Severance - S01E01.mkv",
                 "ProductionYear": 2022},
            ],
            "TotalRecordCount": 3,
        })

    client = JellyfinClient("http://x:8096", "key", transport=httpx.MockTransport(handler))
    rows = await _fetch_items(client, lambda fetched, total: None)

    assert [r["type"] for r in rows] == ["Movie", "Episode"]
    assert "Series" not in seen["IncludeItemTypes"].split(",")


def test_load_state_drops_cached_series_rows(tmp_path, monkeypatch):
    cache = tmp_path / "jellyfin_id.json"
    cache.write_text(json.dumps({"rows": [
        {"type": "Series", "path": "/Serier/Severance", "file": "Severance"},
        {"type": "Movie", "path": "/Filmer/Dune/Dune.mkv", "file": "Dune.mkv"},
    ]}), encoding="utf-8")
    monkeypatch.setattr(jid, "_ID_FILE", cache)

    state = jid._load_state()

    assert [r["path"] for r in state["rows"]] == ["/Filmer/Dune/Dune.mkv"]


def test_migrate_cached_series_rewrites_cache_and_prunes_scans(tmp_path, monkeypatch):
    """A server with an old data volume migrates on startup: the Series rows
    are dropped from the file and their scan entries are pruned."""
    import app.subtitles as sub

    cache = tmp_path / "jellyfin_id.json"
    cache.write_text(json.dumps({"rows": [
        {"type": "Series", "path": "/Serier/Severance", "file": "Severance"},
        {"type": "Movie", "path": "/Filmer/Dune/Dune.mkv", "file": "Dune.mkv"},
    ]}), encoding="utf-8")
    monkeypatch.setattr(jid, "_ID_FILE", cache)
    monkeypatch.setattr(jid, "_state", jid._load_state())
    pruned = {}
    monkeypatch.setattr(
        sub, "prune_to_paths",
        lambda paths: pruned.update(paths=paths) or 0,
    )

    assert jid.migrate_cached_series() == 1

    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert [r["type"] for r in saved["rows"]] == ["Movie"]
    assert pruned["paths"] == {"/Filmer/Dune/Dune.mkv"}
    assert jid.migrate_cached_series() == 0  # already migrated, no rewrite needed


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

async def test_run_fetch_cancelled_leaves_non_running_status(monkeypatch):
    started = asyncio.Event()

    async def fake_items(client, progress):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(jid, "get_client", lambda: object())
    monkeypatch.setattr(jid, "_fetch_items", fake_items)
    monkeypatch.setattr(jid, "_save_state", lambda: None)
    monkeypatch.setattr(jid, "log", lambda *a: None)
    monkeypatch.setattr(jid, "_fetch", {"status": "idle", "fetched": 0, "total": 0, "error": None})

    task = asyncio.create_task(jid._run_fetch())
    await started.wait()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert jid._fetch["status"] == "cancelled"
