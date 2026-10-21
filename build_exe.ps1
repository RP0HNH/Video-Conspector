# Build VideoConspector.exe into dist\VideoConspector (run from the project root).
# Needs the environment with project dependencies installed.
$python = if (Test-Path ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" } else { "python" }

& $python -m pip install -q pyinstaller
& $python -m PyInstaller --noconfirm --clean --windowed --name VideoConspector `
    --collect-all faster_whisper --collect-all ctranslate2 `
    --collect-all av --collect-all yt_dlp `
    --collect-all tokenizers --collect-all pytesseract `
    --hidden-import pkg_resources --exclude-module onnxruntime `
    gui.py

if ($LASTEXITCODE -eq 0) {
    Copy-Item .env.example dist\VideoConspector\.env.example -Force
    Write-Host "Done: dist\VideoConspector\VideoConspector.exe"
}


