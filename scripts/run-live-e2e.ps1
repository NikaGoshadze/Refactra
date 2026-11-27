<#
.SYNOPSIS
Run the live end-to-end browser test: a real task from analysis to a draft pull request.

.DESCRIPTION
Uses the real stack, GitHub App and OpenAI, so each run costs model tokens and opens a draft
pull request on the chosen repository (AI-Hub by default). The script:

1. creates a disposable base branch at the pinned AI-Hub commit and fetches it into the clone;
2. starts the protected stack and a host worker in repository-workflow mode;
3. issues a test session for the allowlisted user (written to a file, never printed);
4. runs tests/e2e-live with playwright.live.config.ts and checks the published draft PR;
5. always revokes the session and stops the worker, and unless -KeepPullRequest is given,
   closes the draft PR and deletes both branches.

Results, the worker log, the Playwright trace and screenshots go to .runtime/e2e-live/<timestamp>.

.EXAMPLE
.\scripts\run-live-e2e.ps1
.\scripts\run-live-e2e.ps1 -KeepPullRequest -Task 'Extract ... Use `python.pytest` on that test file.'
#>
[CmdletBinding()]
param(
    # The repository to pick in the workspace; the App must be installed on it.
    [string]$Repository = 'NikaGoshadze/AI-Hub',
    [string]$Source = '../AI-Hub',
    [string]$BaseCommit = '82b3f25a17cf72cf25d92e0e9726818fcec8831d',
    [string]$Task,
    [switch]$AllowWaivers,
    [switch]$KeepPullRequest,
    [switch]$SkipStart
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$runDir = Join-Path $root ".runtime/e2e-live/$stamp"
$sessionFile = Join-Path $runDir 'session.token'
$resultFile = Join-Path $runDir 'result.json'
$workerLog = Join-Path $runDir 'worker.log'
$baseBranch = "e2e/base-$stamp"
$subject = $null
$baseCreated = $false
$worker = $null
$failed = $true

function Invoke-Checked([string]$what, [scriptblock]$command) {
    & $command
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)." }
}

function Get-EnvValue([string]$name) {
    $line = Get-Content (Join-Path $root '.env') | Where-Object { $_ -match "^\s*$name\s*=" } | Select-Object -First 1
    if (-not $line) { return $null }
    return ($line -split '=', 2)[1].Trim().Trim('"').Trim("'")
}

try {
    foreach ($tool in 'docker', 'gh', 'git', 'uv', 'npx') {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "$tool is not on PATH." }
    }
    foreach ($name in 'REFACTRA_OPENAI_API_KEY', 'REFACTRA_ALLOWED_GITHUB_USER_IDS') {
        if (-not (Get-EnvValue $name)) { throw "$name is not set in .env." }
    }
    $key = Join-Path $root '.runtime/secrets/github-app.pem'
    if (-not (Test-Path $key)) { throw 'The GitHub App key .runtime/secrets/github-app.pem is missing.' }
    $Source = (Resolve-Path $Source).Path
    New-Item -ItemType Directory -Path $runDir -Force | Out-Null
    $artifacts = (New-Item -ItemType Directory -Path (Join-Path $runDir 'artifacts') -Force).FullName

    # GitHub's canonical name, which is also how the workspace picker labels it.
    $subject, $subjectId = gh api "repos/$Repository" --jq '.full_name, .id'
    if ($LASTEXITCODE -ne 0 -or -not $subject -or -not $subjectId) {
        throw "Could not find $Repository; check the name and that gh is signed in."
    }
    Write-Host "Creating $baseBranch on $subject at $BaseCommit."
    Invoke-Checked 'Creating the base branch' {
        gh api "repos/$subject/git/refs" -f "ref=refs/heads/$baseBranch" -f "sha=$BaseCommit" > $null
    }
    $baseCreated = $true
    Invoke-Checked 'Fetching the base branch' { git -C $Source fetch --quiet origin $baseBranch }

    # This harness runs many times a day as the same user, so it raises the per-user limits for
    # its own stack unless they are set. REFACTRA_MONTHLY_USD, the overall budget, still applies.
    foreach ($limit in @{ REFACTRA_USER_DAILY_RUNS = '100'; REFACTRA_USER_MONTHLY_USD = '50' }.GetEnumerator()) {
        if (-not [Environment]::GetEnvironmentVariable($limit.Key)) { Set-Item "env:$($limit.Key)" $limit.Value }
    }

    if (-not $SkipStart) {
        Invoke-Checked 'Stopping the Compose worker' { docker compose stop worker }
        $env:REFACTRA_ENABLE_REPOSITORY_ANALYSIS = 'true'
        & (Join-Path $PSScriptRoot 'start-local.ps1') -RepositoryApi
    }

    Invoke-Checked 'Issuing the test session' {
        uv run --project services/backend --env-file .env python -m refactra.e2e_session --issue $sessionFile
    }

    $env:REFACTRA_GITHUB_PRIVATE_KEY_PATH = $key
    $workerArgs = @(
        '-NoProfile', '-File', (Join-Path $PSScriptRoot 'run-worker.ps1'),
        '--repository-workflow', '--candidate-source', $Source, '--candidate-artifacts', $artifacts
    )
    $worker = Start-Process pwsh -ArgumentList $workerArgs -PassThru -NoNewWindow `
        -RedirectStandardOutput $workerLog -RedirectStandardError "$workerLog.err"
    Write-Host "Worker started (log: $workerLog)."

    $env:REFACTRA_E2E_SESSION_FILE = $sessionFile
    $env:REFACTRA_E2E_BASE_BRANCH = $baseBranch
    $env:REFACTRA_E2E_REPOSITORY = $subject
    $env:REFACTRA_E2E_REPOSITORY_ID = $subjectId
    $env:REFACTRA_E2E_RESULT_FILE = $resultFile
    $env:REFACTRA_E2E_OUTPUT = Join-Path $runDir 'playwright'
    $env:REFACTRA_E2E_ALLOW_WAIVERS = if ($AllowWaivers) { '1' } else { '0' }
    if ($Task) { $env:REFACTRA_E2E_TASK = $Task } else { Remove-Item Env:REFACTRA_E2E_TASK -ErrorAction SilentlyContinue }
    Invoke-Checked 'The live browser test' { npx playwright test --config playwright.live.config.ts }

    $result = Get-Content $resultFile -Raw | ConvertFrom-Json
    $pr = gh pr view $result.pullRequestUrl --json isDraft,baseRefName,headRefName,state,url | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw 'Could not read the published pull request.' }
    if (-not $pr.isDraft -or $pr.state -ne 'OPEN' -or $pr.baseRefName -ne $baseBranch) {
        throw "Unexpected pull request state: $($pr | ConvertTo-Json -Compress)"
    }
    $result | Add-Member -NotePropertyName headBranch -NotePropertyValue $pr.headRefName -Force
    $result | ConvertTo-Json | Set-Content $resultFile -Encoding utf8
    Write-Host "Passed: run $($result.runId) published draft $($pr.url) from $($pr.headRefName)."
    $failed = $false
} finally {
    if ($worker -and -not $worker.HasExited) {
        $worker.Kill($true)  # The whole tree: the supervisor, uv and the Python worker.
        $worker.WaitForExit(10000) | Out-Null
        Write-Host 'Worker stopped.'
    }
    if (Test-Path $sessionFile) {
        uv run --project services/backend --env-file .env python -m refactra.e2e_session --revoke $sessionFile
    }
    if ($subject -and -not $KeepPullRequest) {
        $published = if (Test-Path $resultFile) { (Get-Content $resultFile -Raw | ConvertFrom-Json).pullRequestUrl }
        if ($published) {
            gh pr close $published --delete-branch --comment 'Closed by the live end-to-end test cleanup.' *> $null
            if ($LASTEXITCODE -ne 0) { Write-Warning "Could not close $published; close it by hand." }
        }
        if ($baseCreated) {
            gh api -X DELETE "repos/$subject/git/refs/heads/$baseBranch" *> $null
            if ($LASTEXITCODE -ne 0) { Write-Warning "Could not delete $baseBranch; delete it by hand." }
            git -C $Source branch --quiet --delete --remotes "origin/$baseBranch" *> $null
        }
        Write-Host 'Draft pull request and branches cleaned up.'
    } elseif ($KeepPullRequest) {
        Write-Host "Kept the draft pull request and $baseBranch for review."
    }
    if (-not $SkipStart) { Write-Host 'The Compose worker stays stopped; run "docker compose start worker" to resume it.' }
    Write-Host "Run output: $runDir"
    Pop-Location
    if ($failed) { Write-Host 'The live end-to-end test failed; see the output above and the trace.' }
}
