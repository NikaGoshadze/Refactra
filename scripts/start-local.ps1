[CmdletBinding()]
param([switch]$RepositoryApi)

$ErrorActionPreference = 'Stop'
$compose = @('compose', '-f', 'compose.yaml')
$backendServices = @('api', 'worker')
if ($RepositoryApi) {
    $compose += @('-f', 'compose.repository-api.yaml')
    $backendServices = @('api')
}

Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    # Finish building before asking Compose to select an image for the web container.
    docker @compose build web
    if ($LASTEXITCODE -ne 0) { throw 'The web image build failed.' }
    $builtImage = docker image inspect --format '{{.Id}}' refactra-web:local
    if ($LASTEXITCODE -ne 0 -or -not $builtImage) { throw 'Could not identify the built web image.' }

    docker @compose up --build --wait --wait-timeout 120 @backendServices
    if ($LASTEXITCODE -ne 0) { throw 'The local backend did not become ready.' }

    docker @compose up --no-deps --no-build --force-recreate --wait --wait-timeout 90 web
    if ($LASTEXITCODE -ne 0) { throw 'The rebuilt web service did not become ready.' }

    $webContainer = docker @compose ps --quiet web
    if ($LASTEXITCODE -ne 0 -or -not $webContainer) { throw 'Could not find the running web container.' }
    $runningImage = docker inspect --format '{{.Image}}' $webContainer
    if ($LASTEXITCODE -ne 0 -or $runningImage -ne $builtImage) {
        throw 'The running web container does not use the image just built.'
    }
    Write-Host "Local web is healthy and running $runningImage"
    docker @compose port web 3000
    if ($LASTEXITCODE -ne 0) { throw 'Could not find the published web port.' }
} finally {
    Pop-Location
}
