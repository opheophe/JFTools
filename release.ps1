# Tag a release, push it, and create the GitHub release. GitHub Actions then
# builds and publishes the multi-arch image to ghcr.io/opheophe/jftools
# (:X.Y.Z, :X.Y, :latest).
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

# git and gh write ordinary progress ("To https://...", "Enumerating objects")
# to stderr, which Windows PowerShell 5.1 turns into a terminating
# NativeCommandError while "Stop" is in effect — the command still succeeds, so
# the release used to abort halfway through. Relax the preference around them
# and check $LASTEXITCODE instead.
function Invoke-Native {
    param([scriptblock]$Command, [string]$Failure)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Command
        if ($LASTEXITCODE -ne 0) { throw $Failure }
    } finally {
        $ErrorActionPreference = $previous
    }
}

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw "gh (GitHub CLI) is required to create the release. Install it and run 'gh auth login'."
}

if (git tag --list $tag) { throw "Tag $tag already exists." }
if (git status --porcelain) { throw "Working tree is not clean. Commit your changes first." }

Write-Host "Running tests..."
Invoke-Native { & ".\.venv\Scripts\python.exe" -m pytest -q } "Tests failed; aborting release."

Invoke-Native { git tag -a $tag -m "Release $tag" } "git tag failed."
Invoke-Native { git push origin $tag } "git push failed."

Write-Host "Creating GitHub release..."
Invoke-Native { gh release create $tag --title "Release $tag" --generate-notes } "gh release create failed."


Write-Host ""
Write-Host "Released $tag. GitHub Actions will publish:"
Write-Host "  ghcr.io/opheophe/jftools:$Version"
Write-Host "  ghcr.io/opheophe/jftools:latest"
