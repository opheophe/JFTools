"""The version must come from the image, never from the container's env.

Docker copies an image's ENV into a container once, at create time, so a
container recreated from an older config snapshot keeps the old value while the
new code runs underneath (see ``app/version.py``). These guard the two halves of
the arrangement: the Dockerfile bakes the stamp into the image and does not
declare the version as ENV.
"""
from pathlib import Path

DOCKERFILE = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text(
    encoding="utf-8"
)


def test_dockerfile_bakes_the_version_file():
    assert '/app/app/VERSION' in DOCKERFILE
    assert '"$VERSION"' in DOCKERFILE


def test_dockerfile_does_not_freeze_the_version_in_env():
    offenders = [
        line.strip()
        for line in DOCKERFILE.splitlines()
        if line.strip().rstrip("\\").strip().startswith("JFTOOLS_VERSION=")
    ]
    assert not offenders, (
        "JFTOOLS_VERSION must not be set as ENV: Docker freezes container env at "
        f"create time, so a version stored there reports a stale release. {offenders}"
    )
