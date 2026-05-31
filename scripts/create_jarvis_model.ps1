param(
    [string]$ModelName = "jarvis",
    [string]$Modelfile = "config/Modelfile.jarvis",
    [switch]$SwitchSettings
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$ModelfilePath = if ([System.IO.Path]::IsPathRooted($Modelfile)) {
    $Modelfile
} else {
    Join-Path $RepoRoot $Modelfile
}

if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    throw "Ollama is not available on PATH."
}

if (-not (Test-Path -LiteralPath $ModelfilePath)) {
    throw "Modelfile not found: $Modelfile"
}

Push-Location $RepoRoot
try {
    ollama create $ModelName -f $ModelfilePath
} finally {
    Pop-Location
}

if ($SwitchSettings) {
    $settingsPath = Join-Path $RepoRoot "config/settings.json"
    if (-not (Test-Path -LiteralPath $settingsPath)) {
        throw "settings.json not found: $settingsPath"
    }

    $settings = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
    $settings.llm_provider = "ollama"
    $settings.planner_provider = "ollama"
    $settings.llm_model = $ModelName
    $settings.planner_model = $ModelName
    $settings.ollama_model = $ModelName
    $settings.fast_ollama_model = $ModelName
    $settings | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $settingsPath -Encoding UTF8
}

Write-Host "Created Ollama model: $ModelName"
if ($SwitchSettings) {
    Write-Host "Updated config/settings.json to use: $ModelName"
}
