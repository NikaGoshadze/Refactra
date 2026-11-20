[CmdletBinding()]
param([switch]$RepositoryApi)

$ErrorActionPreference = 'Stop'
$compose = @('compose', '-f', 'compose.yaml')
$backendServices = @('api', 'worker')
if ($RepositoryApi) {
    $compose += @('-f', 'compose.repository-api.yaml')
    $backendServices = @('api')
}

function Get-LocalImageId([string]$imageTag) {
    $imageId = docker image inspect --format '{{.Id}}' $imageTag
    if ($LASTEXITCODE -ne 0 -or -not $imageId) {
        throw "Could not identify the built image $imageTag."
    }
    return $imageId
}

function Assert-ServiceImage([string]$service, [string]$expectedImage) {
    $container = docker @compose ps --quiet $service
    if ($LASTEXITCODE -ne 0 -or -not $container) {
        throw "Could not find the running $service container."
    }
    $runningImage = docker inspect --format '{{.Image}}' $container
    if ($LASTEXITCODE -ne 0 -or $runningImage -ne $expectedImage) {
        throw "The running $service container does not use the image just built."
    }
}

Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    # Build before Compose selects images. The API, worker, and migrate share one image.
    docker @compose build api web
    if ($LASTEXITCODE -ne 0) { throw 'The local image build failed.' }
    $backendImage = Get-LocalImageId refactra-backend:local
    $webImage = Get-LocalImageId refactra-web:local

    docker @compose up --no-build --wait --wait-timeout 120 @backendServices
    if ($LASTEXITCODE -ne 0) { throw 'The local backend did not become ready.' }
    foreach ($service in $backendServices) {
        Assert-ServiceImage $service $backendImage
    }

    docker @compose up --no-deps --no-build --force-recreate --wait --wait-timeout 90 web
    if ($LASTEXITCODE -ne 0) { throw 'The rebuilt web service did not become ready.' }

    Assert-ServiceImage web $webImage
    Write-Host "Local backend and web are running the built images."
    docker @compose port web 3000
    if ($LASTEXITCODE -ne 0) { throw 'Could not find the published web port.' }
} finally {
    Pop-Location
}
