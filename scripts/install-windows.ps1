param(
    [switch]$Editable
)

$ErrorActionPreference = "Stop"

if ($Editable) {
    py -m pip install -e ".[dev]"
} else {
    if (Get-Command pipx -ErrorAction SilentlyContinue) {
        pipx install --force ai-orchestrator
    } else {
        py -m pip install --user ai-orchestrator
    }
}

orch doctor
