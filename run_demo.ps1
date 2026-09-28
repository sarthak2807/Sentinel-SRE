# run_demo.ps1
# Starts both processes and tails them together in PowerShell. Ctrl-C stops everything.

if (-not (Test-Path .env)) {
    if (Test-Path .env.example) {
        Copy-Item .env.example .env
        Write-Host "Created .env from .env.example"
    } else {
        Write-Error "No .env or .env.example found."
        Exit 1
    }
}

Write-Host "Setting up vector store..."
python scripts/setup_vector_store.py

Write-Host "Starting Sample service in background..."
$ServiceProcess = Start-Process python -ArgumentList "sample_service/app.py" -NoNewWindow -PassThru

Start-Sleep -Seconds 2

Write-Host "Starting Sentinel server in background..."
$AgentProcess = Start-Process python -ArgumentList "-m sentinel.server" -NoNewWindow -PassThru

Write-Host ""
Write-Host "  Sample service  http://localhost:8000/health"
Write-Host "  Sentinel        http://localhost:8080"
Write-Host ""
Write-Host "  Wait ~90s for a baseline, then:"
Write-Host "    python scripts/inject_fault.py --type n_plus_one"
Write-Host ""

try {
    # Keep running to let user terminate both with Ctrl-C in this console
    while ($true) {
        if ($ServiceProcess.HasExited) {
            Write-Warning "Sample Service process exited unexpectedly!"
            break
        }
        if ($AgentProcess.HasExited) {
            Write-Warning "Sentinel Agent process exited unexpectedly!"
            break
        }
        Start-Sleep -Seconds 1
    }
}
finally {
    Write-Host "`nStopping background processes..."
    if ($ServiceProcess -and -not $ServiceProcess.HasExited) {
        Stop-Process -Id $ServiceProcess.Id -Force -ErrorAction SilentlyContinue
    }
    if ($AgentProcess -and -not $AgentProcess.HasExited) {
        Stop-Process -Id $AgentProcess.Id -Force -ErrorAction SilentlyContinue
    }
}
