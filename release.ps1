# Tag a release and push it. GitHub Actions then builds and publishes the
# multi-arch image to ghcr.io/opheophe/medserv (:X.Y.Z, :X.Y, :latest).
#
#   .\release.ps1 1.0.0
#
#Requires -Version 5.0
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Version
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Version must be semver without a leading 'v', e.g. 1.0.0"
}
$tag = "v$Version"

if (git tag --list $tag) { throw "Tag $tag already exists." }
if (git status --porcelain) { throw "Working tree is not clean. Commit your changes first." }

Write-Host "Running tests..."
& ".\.venv\Scripts\python.exe" -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "Tests failed; aborting release." }

git tag -a $tag -m "Release $tag"
git push origin $tag
Write-Host ""
Write-Host "Pushed $tag. GitHub Actions will publish:"
Write-Host "  ghcr.io/opheophe/medserv:$Version"
Write-Host "  ghcr.io/opheophe/medserv:latest"
