[CmdletBinding()]
param(
    [string]$SpecifyCli,
    [string]$Uv,
    [string]$SpecifyVersion = '1.4.2'
)

$ErrorActionPreference = 'Stop'
$Guard = Join-Path $PSScriptRoot '../skills/speckit-start/guard.py'
$Python = (Get-Command python -CommandType Application | Select-Object -First 1).Source

if ($SpecifyCli -and $Uv) { throw 'Choose either an existing Specify CLI or a fresh uv install.' }
if (-not $SpecifyCli -and -not $Uv) {
    $Command = Get-Command specify -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $Command) {
        throw 'Specify CLI is not on PATH. Pass its verified executable path with -SpecifyCli.'
    }
    $SpecifyCli = $Command.Source
}
if ($SpecifyCli) { $SpecifyCli = (Resolve-Path -LiteralPath $SpecifyCli).ProviderPath }
if ($Uv) { $Uv = (Resolve-Path -LiteralPath $Uv).ProviderPath }
$ScriptType = if ($env:OS -eq 'Windows_NT') { 'ps' } else { 'sh' }
$IntegrationOptions = '--integration-options=--events false'
$SkillPaths = @{ codex = '.agents/skills'; claude = '.claude/skills' }

function Invoke-Specify {
    $Start = [Diagnostics.ProcessStartInfo]::new($SpecifyCli)
    $Start.UseShellExecute = $false
    $Start.WorkingDirectory = $PWD.Path
    $Start.RedirectStandardOutput = $true
    $Start.RedirectStandardError = $true
    $Start.Environment.Clear()
    foreach ($Key in @(
        'PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'SYSTEMDRIVE',
        'HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP', 'TMPDIR',
        'XDG_CONFIG_HOME', 'XDG_DATA_HOME'
    )) {
        $Value = [Environment]::GetEnvironmentVariable($Key, 'Process')
        if ($null -ne $Value) { $Start.Environment[$Key] = $Value }
    }
    $Start.Environment['PYTHONDONTWRITEBYTECODE'] = '1'
    foreach ($Argument in $args) { $Start.ArgumentList.Add($Argument) }
    $Process = [Diagnostics.Process]::Start($Start)
    try {
        $Output = $Process.StandardOutput.ReadToEndAsync()
        $Errors = $Process.StandardError.ReadToEndAsync()
        $Process.WaitForExit()
        $null = $Errors.GetAwaiter().GetResult()
        if ($Process.ExitCode -ne 0) {
            throw "Specify fixture command failed with exit code $($Process.ExitCode)"
        }
        $Output.GetAwaiter().GetResult()
    } finally {
        $Process.Dispose()
    }
}

function Assert-Setup([string]$DefaultAgent, [string[]]$ExpectedAgents) {
    $Status = Invoke-Specify integration status --json | Out-String | ConvertFrom-Json
    if ($Status.status -ne 'ok' -or $Status.default_integration -ne $DefaultAgent) {
        throw "Unexpected health or default integration: $($Status | ConvertTo-Json -Depth 8)"
    }
    foreach ($ActualAgents in @('installed_integrations', 'recorded_installed_integrations')) {
        $Actual = ($Status.$ActualAgents | Sort-Object) -join ','
        $Expected = ($ExpectedAgents | Sort-Object) -join ','
        if ($Actual -ne $Expected) {
            throw "Unexpected $ActualAgents for $DefaultAgent"
        }
    }
    foreach ($Agent in $ExpectedAgents) {
        if (-not $Status.manifests.$Agent.readable) { throw "Missing manifest for $Agent" }
        foreach ($Step in @('specify', 'clarify', 'plan', 'tasks')) {
            $Skill = Join-Path $SkillPaths[$Agent] "speckit-$Step/SKILL.md"
            $NamePattern = '(?m)^name:\s*["'']?speckit-{0}["'']?\s*$' -f $Step
            if ((Get-Content -Raw -LiteralPath $Skill) -notmatch $NamePattern) {
                throw "Invalid skill file: $Skill"
            }
        }
    }
}

function Get-TreeSnapshot {
    $Files = @{}
    foreach ($File in (Get-ChildItem -LiteralPath . -Recurse -Force -File | Get-FileHash)) {
        $Relative = [IO.Path]::GetRelativePath($PWD.Path, $File.Path).Replace('\', '/')
        $Files[$Relative] = $File.Hash
    }
    $Files
}

function Invoke-Guard {
    $Json = & $Python -I -B $Guard @args
    if ($LASTEXITCODE -ne 0) { throw "Setup guard failed with exit code $LASTEXITCODE" }
    $Json | ConvertFrom-Json
}

function Assert-Tree($Before, $After) {
    if ($Before.Count -ne $After.Count) { throw 'Unexpected number of files in fixture' }
    foreach ($Path in $Before.Keys) {
        if ($After[$Path] -ne $Before[$Path]) { throw "Unexpected fixture change: $Path" }
    }
}

function Invoke-ProjectStep($Operation, $Agent, $Creates, $Updates) {
    $Arguments = @(
        'project', "--root=$($PWD.Path)", "--workspace=$($PWD.Path)",
        "--executable=$SpecifyCli", "--operation=$Operation", "--agent=$Agent"
    )
    $Arguments += @($Creates | ForEach-Object { "--create=$_" })
    $Arguments += @($Updates | ForEach-Object { "--update=$_" })
    $Before = Get-TreeSnapshot
    $Plan = Invoke-Guard @Arguments
    Assert-Tree $Before (Get-TreeSnapshot)
    if ($Plan.status -ne 'plan') { throw "Expected a plan for $Operation $Agent" }
    $Applied = Invoke-Guard @Arguments "--approve=$($Plan.plan_id)"
    if ($Applied.status -ne 'command-completed') { throw "Expected completion for $Operation $Agent" }
    $After = Get-TreeSnapshot
    $ActualCreates = @($After.Keys | Where-Object { -not $Before.ContainsKey($_) })
    $ActualUpdates = @($Before.Keys | Where-Object { $Before[$_] -ne $After[$_] })
    # Approved updates can be no-ops, for example when init-options already records this CLI version.
    if ((($ActualCreates | Sort-Object) -join ',') -ne (($Creates | Sort-Object) -join ',') -or
        @($ActualUpdates | Where-Object { $_ -notin $Updates -or -not $After.ContainsKey($_) }).Count) {
        throw "Unplanned filesystem change during $Operation $Agent"
    }
}

$TempParent = (Resolve-Path -LiteralPath ([System.IO.Path]::GetTempPath())).ProviderPath
$TestRoot = Join-Path $TempParent ('Spec Kit 검증 ' + [guid]::NewGuid())
$IsolatedHome = Join-Path $TestRoot '임시 사용자 홈'
$FixtureBin = Join-Path $TestRoot '에이전트 탐색'
$FixtureTemp = Join-Path $TestRoot '임시 파일'
$Overrides = @{
    HOME = $IsolatedHome; USERPROFILE = $IsolatedHome
    APPDATA = (Join-Path $IsolatedHome 'Roaming')
    LOCALAPPDATA = (Join-Path $IsolatedHome 'Local')
    XDG_CONFIG_HOME = (Join-Path $IsolatedHome 'config')
    XDG_DATA_HOME = (Join-Path $IsolatedHome 'data')
    SPECIFY_INIT_DIR = $null; SPECKIT_INTEGRATION_CATALOG_URL = $null
    GIT_DIR = $null; GIT_WORK_TREE = $null
    PYTHONPATH = $null; PYTHONHOME = $null
    TEMP = $FixtureTemp; TMP = $FixtureTemp; TMPDIR = $FixtureTemp
    PATH = "$FixtureBin$([IO.Path]::PathSeparator)$env:PATH"
}
$SavedEnvironment = @{}
New-Item -ItemType Directory -Path $TestRoot | Out-Null
Push-Location -LiteralPath $TestRoot
try {
    foreach ($Entry in $Overrides.GetEnumerator()) {
        $SavedEnvironment[$Entry.Key] = [Environment]::GetEnvironmentVariable($Entry.Key, 'Process')
        [Environment]::SetEnvironmentVariable($Entry.Key, $Entry.Value, 'Process')
    }
    New-Item -ItemType Directory -Path $IsolatedHome, $FixtureBin, $FixtureTemp | Out-Null
    foreach ($Agent in @('codex', 'claude')) {
        if ($env:OS -eq 'Windows_NT') {
            Set-Content -LiteralPath (Join-Path $FixtureBin "$Agent.cmd") -Value '@exit /b 0'
        } else {
            $AgentFixture = Join-Path $FixtureBin $Agent
            Set-Content -LiteralPath $AgentFixture -Value "#!/bin/sh`nexit 0"
            & chmod +x $AgentFixture
            if ($LASTEXITCODE -ne 0) { throw 'Could not create executable agent fixture' }
        }
    }
    if ($Uv) {
        $Tools = Join-Path $IsolatedHome 'uv tools'
        $ToolBin = Join-Path $IsolatedHome 'uv bin'
        $Cache = Join-Path $IsolatedHome 'uv cache'
        $InstallArguments = @(
            'install-cli', "--root=$TestRoot", "--executable=$Uv", "--version=$SpecifyVersion",
            "--tools=$Tools", "--bin=$ToolBin", "--cache=$Cache"
        )
        $InstallPlan = Invoke-Guard @InstallArguments
        foreach ($Path in @($Tools, $ToolBin, $Cache)) {
            if (Test-Path -LiteralPath $Path) { throw 'CLI install planning wrote to the isolated home' }
        }
        $Installed = Invoke-Guard @InstallArguments "--approve=$($InstallPlan.plan_id)"
        $LauncherName = if ($env:OS -eq 'Windows_NT') { 'specify.exe' } else { 'specify' }
        $SpecifyCli = Join-Path $ToolBin $LauncherName
        if ($Installed.status -ne 'command-completed' -or -not (Test-Path -LiteralPath $SpecifyCli)) {
            throw 'Fresh install did not provide the expected launcher'
        }
        if ($env:OS -ne 'Windows_NT') {
            $BackingCli = Join-Path $Tools 'specify-cli/bin/specify'
            $ResolvedLauncher = (Get-Item -LiteralPath $SpecifyCli).ResolveLinkTarget($true)
            if ($ResolvedLauncher.FullName -cne $BackingCli) { throw 'Unexpected uv launcher target' }
            $SpecifyCli = $BackingCli
        }
        $VersionOutput = Invoke-Specify version | Out-String
        if ($VersionOutput -notmatch [regex]::Escape($SpecifyVersion)) { throw 'Installed CLI version mismatch' }
        Write-Output "PASS: installed Specify $SpecifyVersion in the isolated user home"
    } else {
        Invoke-Specify version | Out-Null
    }
    foreach ($Pair in @(@('codex', 'claude'), @('claude', 'codex'))) {
        $First, $Second = $Pair
        $Scaffold = Join-Path $TestRoot "미리 보기 $First"
        $Project = Join-Path $TestRoot "테스트 프로젝트 $First"
        New-Item -ItemType Directory -Path $Scaffold, $Project | Out-Null
        Push-Location -LiteralPath $Scaffold
        try {
            Invoke-Specify init --here --non-interactive --integration $First --script $ScriptType $IntegrationOptions | Out-Null
            $InitPreview = Get-TreeSnapshot
            Invoke-Specify integration install $Second $IntegrationOptions | Out-Null
            $DualPreview = Get-TreeSnapshot
        } finally {
            Pop-Location
        }
        $AddCreates = @($DualPreview.Keys | Where-Object { -not $InitPreview.ContainsKey($_) })
        $AddUpdates = @('.specify/integration.json', '.specify/init-options.json')
        Push-Location -LiteralPath $Project
        try {
            $ExistingSettings = @('.codex/config.toml', '.claude/settings.json')
            if ($First -eq 'claude') {
                New-Item -ItemType Directory -Path '.codex', '.claude' | Out-Null
                Set-Content -LiteralPath $ExistingSettings[0] -Value '# User-owned Codex config' -NoNewline
                Set-Content -LiteralPath $ExistingSettings[1] -Value '{"env":{"SMOKE_TEST":"1"}}' -NoNewline
            }
            $SettingsBefore = Get-ChildItem -LiteralPath . -Force -Recurse -File | Get-FileHash
            Invoke-ProjectStep 'init' $First $InitPreview.Keys @()
            Assert-Setup $First @($First)
            if (Test-Path -LiteralPath $SkillPaths[$Second]) {
                throw "Initialization unexpectedly created $Second skills"
            }
            Write-Output "PASS: initialized $First only"

            New-Item -ItemType Directory -Path 'specs/001-example' -Force | Out-Null
            Set-Content -LiteralPath 'specs/001-example/spec.md' -Value 'Existing SDD document'
            $FilesToPreserve = @($SettingsBefore) + @(
                Get-FileHash -LiteralPath 'specs/001-example/spec.md', '.specify/memory/constitution.md'
                Get-ChildItem -LiteralPath '.specify/templates', $SkillPaths[$First] -Recurse -Force -File |
                    Get-FileHash
            )
            Invoke-ProjectStep 'add' $Second $AddCreates $AddUpdates
            Assert-Setup $First @($First, $Second)
            foreach ($File in $FilesToPreserve) {
                if ($File -and (Get-FileHash -LiteralPath $File.Path).Hash -ne $File.Hash) {
                    throw "Existing file was modified: $($File.Path)"
                }
            }
            Write-Output "PASS: $First + $Second kept the default, settings and existing SDD files"

            $Before = Get-TreeSnapshot
            foreach ($Agent in $Pair) {
                $NoOp = Invoke-Guard project "--root=$Project" "--workspace=$Project" `
                    "--executable=$SpecifyCli" --operation=add "--agent=$Agent"
                if ($NoOp.status -ne 'already-registered') { throw "Repeated setup planned writes for $Agent" }
            }
            Assert-Setup $First @($First, $Second)
            Assert-Tree $Before (Get-TreeSnapshot)
            Write-Output "PASS: repeated status checks were read-only ($First default)"
        } finally {
            Pop-Location
        }
    }
} finally {
    foreach ($Entry in $SavedEnvironment.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($Entry.Key, $Entry.Value, 'Process')
    }
    Pop-Location
    $ResolvedRoot = (Resolve-Path -LiteralPath $TestRoot).ProviderPath
    $Scratch = Get-Item -Force -LiteralPath $ResolvedRoot
    if ($ResolvedRoot -eq $TestRoot -and (Split-Path -Parent $ResolvedRoot) -eq $TempParent -and
        -not ($Scratch.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        Remove-Item -LiteralPath $ResolvedRoot -Recurse -Force
    }
}
