from app.media import _image_ref, _image_refs, _item_details, _item_label


def test_item_label_episode_prefixes_series():
    assert _item_label({"Type": "Episode", "Name": "Pilot", "SeriesName": "Show"}) == "Show — Pilot"


def test_item_label_movie_uses_name():
    assert _item_label({"Type": "Movie", "Name": "Dune"}) == "Dune"


def test_image_ref_uses_series_poster_for_episode():
    np = {"Type": "Episode", "Id": "ep1", "SeriesId": "se1", "SeriesPrimaryImageTag": "t"}
    assert _image_ref(np) == {"item_id": "se1", "tag": "t"}


def test_image_ref_uses_own_primary_for_movie():
    np = {"Type": "Movie", "Id": "m1", "ImageTags": {"Primary": "p"}}
    assert _image_ref(np) == {"item_id": "m1", "tag": "p"}


def test_image_ref_falls_back_to_series_poster():
    np = {"Type": "Episode", "Id": "ep1", "SeriesId": "se1", "SeriesPrimaryImageTag": "t", "ImageTags": {}}
    assert _image_ref(np) == {"item_id": "se1", "tag": "t"}


def test_image_ref_none_without_images():
    assert _image_ref({"Type": "Movie", "Id": "m1", "ImageTags": {}}) is None


def test_image_refs_movie_backdrop_and_still():
    refs = _image_refs({
        "Type": "Movie",
        "Id": "m1",
        "ImageTags": {"Primary": "p"},
        "BackdropImageTags": ["b1"],
        "ThumbImageTags": ["t1"],
    })
    assert refs["primary"] == {"item_id": "m1", "tag": "p"}
    assert refs["backdrop"] == {"item_id": "m1", "tag": "b1"}
    assert refs["thumb"] == {"item_id": "m1", "tag": "t1"}


def test_image_refs_episode_falls_back_to_parent_images():
    refs = _image_refs({
        "Type": "Episode",
        "Id": "ep1",
        "SeriesId": "se1",
        "SeriesPrimaryImageTag": "sp",
        "ParentBackdropItemId": "pb",
        "ParentBackdropImageTags": ["bt"],
        "ParentThumbItemId": "pt",
        "ParentThumbImageTags": ["tt"],
    })
    assert refs["primary"] == {"item_id": "se1", "tag": "sp"}
    assert refs["backdrop"] == {"item_id": "pb", "tag": "bt"}
    assert refs["thumb"] == {"item_id": "pt", "tag": "tt"}


def test_item_details_shapes_fields():
    d = _item_details({
        "Id": "m1",
        "Type": "Movie",
        "Name": "Dune",
        "ProductionYear": 2021,
        "Overview": "  A mythic hero.  ",
        "Genres": ["Sci-Fi", "Adventure"],
        "CommunityRating": 8.15,
        "CriticRating": 83,
        "OfficialRating": "PG-13",
        "RunTimeTicks": 15_600_000_000,
        "Studios": [{"Name": "Legendary"}],
        "People": [
            {"Name": "Denis V.", "Type": "Director"},
            {"Name": "Timothée C.", "Type": "Actor"},
        ],
        "ImageTags": {"Primary": "p"},
    })
    assert d["name"] == "Dune"
    assert d["overview"] == "A mythic hero."
    assert d["runtime_s"] == 1560
    assert d["studios"] == ["Legendary"]
    assert d["directors"] == ["Denis V."]
    assert d["cast"] == ["Timothée C."]
    assert d["images"]["primary"] == {"item_id": "m1", "tag": "p"}


def test_item_details_episode_numbering():
    d = _item_details({"Type": "Episode", "Name": "Pilot", "SeriesName": "Show", "ParentIndexNumber": 1, "IndexNumber": 3})
    assert (d["series"], d["season"], d["episode"]) == ("Show", 1, 3)
    assert d["overview"] == ""
    assert d["runtime_s"] == 0
