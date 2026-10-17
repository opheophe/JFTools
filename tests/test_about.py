from app.about import _is_semver, _newer, _parse_version, current_version


def test_current_version_strips_v_prefix(monkeypatch):
    monkeypatch.setenv("JFTOOLS_VERSION", "v1.3.2")
    assert current_version() == "1.3.2"


def test_current_version_defaults_to_dev(monkeypatch):
    monkeypatch.delenv("JFTOOLS_VERSION", raising=False)
    assert current_version() == "dev"


def test_parse_version_pads_to_three_parts():
    assert _parse_version("1.3.2") == (1, 3, 2)
    assert _parse_version("1.3") == (1, 3, 0)
    assert _parse_version("1.3.10") == (1, 3, 10)


def test_is_semver():
    assert _is_semver("1.3.2")
    assert _is_semver("v1.3.2")
    assert not _is_semver("dev")
    assert not _is_semver("main")
    assert not _is_semver("")


def test_newer_compares_semver_only():
    assert _newer("1.3.3", "1.3.2")
    assert not _newer("1.3.2", "1.3.2")
    assert not _newer("1.3.1", "1.3.2")
    # Non-semver current versions (dev / edge) never flag an update.
    assert not _newer("1.3.3", "dev")
    assert not _newer("1.3.3", "main")
