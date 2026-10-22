from app.about import _is_semver, _newer, _parse_version, current_version
from app.version import current_version as version_file_version


def test_current_version_strips_v_prefix(monkeypatch, tmp_path):
    monkeypatch.setattr("app.version.VERSION_FILE", tmp_path / "missing")
    monkeypatch.setenv("JFTOOLS_VERSION", "v1.3.2")
    assert current_version() == "1.3.2"


def test_current_version_defaults_to_dev(monkeypatch, tmp_path):
    monkeypatch.setattr("app.version.VERSION_FILE", tmp_path / "missing")
    monkeypatch.delenv("JFTOOLS_VERSION", raising=False)
    assert current_version() == "dev"


def test_baked_version_file_wins_over_a_stale_env(tmp_path, monkeypatch):
    # The case that matters: a container recreated from an older config keeps
    # the old JFTOOLS_VERSION env while the 1.23.0 layers run underneath.
    stamp = tmp_path / "VERSION"
    stamp.write_text("1.23.0\n", encoding="utf-8")
    monkeypatch.setattr("app.version.VERSION_FILE", stamp)
    monkeypatch.setenv("JFTOOLS_VERSION", "v1.21.0")

    assert version_file_version() == "1.23.0"


def test_blank_version_file_falls_back_to_env(tmp_path, monkeypatch):
    stamp = tmp_path / "VERSION"
    stamp.write_text("\n", encoding="utf-8")
    monkeypatch.setattr("app.version.VERSION_FILE", stamp)
    monkeypatch.setenv("JFTOOLS_VERSION", "v1.22.0")

    assert version_file_version() == "1.22.0"


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
