#!/bin/sh
# Tag a release and push it. GitHub Actions then builds and publishes the
# multi-arch image to ghcr.io/opheophe/medserv (:X.Y.Z, :X.Y, :latest).
#
#   ./release.sh 1.0.0
set -eu
cd "$(dirname "$0")"

version="${1:-}"
if [ -z "$version" ]; then
    echo "usage: ./release.sh 1.2.3" >&2
    exit 1
fi
case "$version" in
    *[!0-9.]*|.*|*.) echo "Version must look like 1.2.3" >&2; exit 1 ;;
esac
tag="v$version"

if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
    echo "Tag $tag already exists." >&2
    exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
    echo "Working tree is not clean. Commit your changes first." >&2
    exit 1
fi

echo "Running tests..."
python -m pytest -q

git tag -a "$tag" -m "Release $tag"
git push origin "$tag"
echo ""
echo "Pushed $tag. GitHub Actions will publish:"
echo "  ghcr.io/opheophe/medserv:$version"
echo "  ghcr.io/opheophe/medserv:latest"
