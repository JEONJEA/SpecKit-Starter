[CmdletBinding()]
param([string]$SpecifyCli)

$ErrorActionPreference = 'Stop'

if (-not $SpecifyCli) {
    $Command = Get-Command specify -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $Command) {
        throw 'Specify CLI is not on PATH. Pass its verified executable path with -SpecifyCli.'
    }
    $SpecifyCli = $Command.Source
}
$SpecifyCli = (Resolve-Path -LiteralPath $SpecifyCli).ProviderPath
$ScriptType = if ($env:OS -eq 'Windows_NT') { 'ps' } else { 'sh' }
$IntegrationOptions = '--integration-options=--events false'
$SkillPaths = @{ codex = '.agents/skills'; claude = '.claude/skills' }

function Invoke-Specify {
    $Output = & $SpecifyCli @args
    if ($LASTEXITCODE -ne 0) {
        throw "specify $args failed with exit code ${LASTEXITCODE}:`n$($Output -join "`n")"
    }
    $Output
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
    (Get-ChildItem -LiteralPath . -Recurse -Force -File | Sort-Object FullName |
        Get-FileHash | ForEach-Object { "$($_.Path) $($_.Hash)" }) -join "`n"
}

Invoke-Specify version | Out-Null
$TempParent = (Resolve-Path -LiteralPath ([System.IO.Path]::GetTempPath())).ProviderPath
$TestRoot = Join-Path $TempParent ('speckit-start-test-' + [guid]::NewGuid())
$SavedInitDir = $env:SPECIFY_INIT_DIR
New-Item -ItemType Directory -Path $TestRoot | Out-Null
Push-Location -LiteralPath $TestRoot
try {
    $env:SPECIFY_INIT_DIR = $null
    foreach ($Pair in @(@('codex', 'claude'), @('claude', 'codex'))) {
        $First, $Second = $Pair
        New-Item -ItemType Directory -Path $First | Out-Null
        Push-Location -LiteralPath $First
        try {
            $InitOptions = @('--here', '--non-interactive', '--integration', $First, '--script', $ScriptType, $IntegrationOptions)
            $ExistingSettings = @('.codex/config.toml', '.claude/settings.json')
            if ($First -eq 'claude') {
                New-Item -ItemType Directory -Path '.codex', '.claude' | Out-Null
                Set-Content -LiteralPath $ExistingSettings[0] -Value '# User-owned Codex config' -NoNewline
                Set-Content -LiteralPath $ExistingSettings[1] -Value '{"env":{"SMOKE_TEST":"1"}}' -NoNewline
                $InitOptions += '--force'
            }
            $SettingsBefore = Get-ChildItem -LiteralPath . -Force -Recurse -File | Get-FileHash
            Invoke-Specify init @InitOptions | Out-Null
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
            Invoke-Specify integration install $Second $IntegrationOptions | Out-Null
            Assert-Setup $First @($First, $Second)
            foreach ($File in $FilesToPreserve) {
                if ($File -and (Get-FileHash -LiteralPath $File.Path).Hash -ne $File.Hash) {
                    throw "Existing file was modified: $($File.Path)"
                }
            }
            Write-Output "PASS: $First + $Second kept the default, settings and existing SDD files"

            $Before = Get-TreeSnapshot
            Assert-Setup $First @($First, $Second)
            Assert-Setup $First @($First, $Second)
            if ((Get-TreeSnapshot) -ne $Before) { throw 'Repeated verification wrote to the project' }
            Write-Output "PASS: repeated status checks were read-only ($First default)"
        } finally {
            Pop-Location
        }
    }
} finally {
    $env:SPECIFY_INIT_DIR = $SavedInitDir
    Pop-Location
    $ResolvedRoot = (Resolve-Path -LiteralPath $TestRoot).ProviderPath
    if ($ResolvedRoot -eq $TestRoot -and (Split-Path -Parent $ResolvedRoot) -eq $TempParent) {
        Remove-Item -LiteralPath $ResolvedRoot -Recurse -Force
    }
}
