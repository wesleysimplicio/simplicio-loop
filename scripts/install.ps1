# simplicio-tasks installer (thin launcher -> scripts/install_lib.py)
# Usage: pwsh scripts/install.ps1 <runtime> [-Global] [-Target DIR]
$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = (Get-Command python3 -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $py) {
  Write-Error "python3 is required (the skills, hooks, and installer are cross-platform Python)."
  exit 1
}
# pass args through; map -Global/-Target to the python flags
$pyArgs = @("$dir/install_lib.py")
foreach ($a in $args) {
  switch ($a) {
    "-Global" { $pyArgs += "--global" }
    "-Target" { $pyArgs += "--target" }
    default   { $pyArgs += $a }
  }
}
& $py @pyArgs
