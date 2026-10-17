#!/bin/sh
# Tag a release, push it, and create the GitHub release. GitHub Actions then
# builds and publishes the multi-arch image to ghcr.io/opheophe/jftools
# (:X.Y.Z, :X.Y, :latest).
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

if ! command -v gh >/dev/null 2>&1; then
    echo "gh (GitHub CLI) is required to create the release. Install it and run 'gh auth login'." >&2
    exit 1
fi

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

echo "Creating GitHub release..."
gh release create "$tag" --title "Release $tag" --generate-notes

echo ""
echo "Released $tag. GitHub Actions will publish:"
echo "  ghcr.io/opheophe/jftools:$version"
echo "  ghcr.io/opheophe/jftools:latest"
