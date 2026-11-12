param([switch]$Diarization)
$ErrorActionPreference = 'Stop'
Push-Location (Join-Path $PSScriptRoot 'model')
try {
    $python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (!(Test-Path -LiteralPath $python)) {
        python -m venv --without-pip (Join-Path $PSScriptRoot '.venv')
        if ($LASTEXITCODE) { throw 'Could not create the project Python environment.' }
    }
    $requirements = if ($Diarization) { 'requirements-diarization.txt' } else { 'requirements.txt' }
    $stamp = Join-Path $PSScriptRoot '.venv\callguard-dependencies.txt'
    $signature = "$requirements $((Get-FileHash requirements.txt).Hash) $((Get-FileHash requirements-diarization.txt).Hash)"
    if (!(Test-Path $stamp) -or (Get-Content $stamp -Raw).Trim() -ne $signature) {
        python -m pip --python $python install -r $requirements
        if ($LASTEXITCODE) { throw 'Backend dependencies could not be installed.' }
        python -m pip --python $python check
        if ($LASTEXITCODE) { throw 'Conflicting dependencies in the project environment.' }
        & $python -c "import stt_service, nlp_processor, audio_processor, main"
        if ($LASTEXITCODE) { throw 'Backend import check failed.' }
        Set-Content $stamp $signature
    }
    & $python -c "import config, sys; sys.exit(0 if config.GROQ_API_KEY else 'Set GROQ_API_KEY in model/.env before starting call analysis.')"
    if ($LASTEXITCODE) { throw 'Missing Groq configuration.' }
    & $python main.py
    if ($LASTEXITCODE) { throw 'Backend stopped with an error.' }
} finally {
    Pop-Location
}
