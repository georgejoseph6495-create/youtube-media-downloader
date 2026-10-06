<#
.SYNOPSIS
    YouTube Media Downloader — first-time setup for Windows.

.DESCRIPTION
    1. Checks Python, yt-dlp, FFmpeg, FFprobe.
    2. Installs Python packages from backend/requirements.txt.
    3. If FFmpeg is NOT found on PATH or locally, downloads a static Windows
       build into tools/ffmpeg/ and verifies it.
    4. Shows a final status summary.

    Run from the project root:
        .\setup.ps1
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-OK   { param($msg) Write-Host "  [OK]  $msg" -ForegroundColor Green }
function Write-MISS { param($msg) Write-Host "  [!!]  $msg" -ForegroundColor Red }
function Write-INFO { param($msg) Write-Host "        $msg" -ForegroundColor Cyan }
function Write-STEP { param($msg) Write-Host "`n>>> $msg" -ForegroundColor Yellow }

function Find-Exe {
    param([string]$Name)
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

$ProjectRoot = $PSScriptRoot
$BackendDir  = Join-Path $ProjectRoot "backend"
$ToolsDir    = Join-Path $ProjectRoot "tools\ffmpeg"

Write-Host ""
Write-Host "============================================" -ForegroundColor DarkGray
Write-Host "  YouTube Media Downloader -- Setup" -ForegroundColor White
Write-Host "============================================" -ForegroundColor DarkGray

# 1. Python
Write-STEP "Checking Python..."
$pythonExe = Find-Exe "python"
if (-not $pythonExe) { $pythonExe = Find-Exe "python3" }

if ($pythonExe) {
    $pyVer = & $pythonExe --version 2>&1
    Write-OK "Python found: $pyVer"
} else {
    Write-MISS "Python not found."
    Write-INFO "Install Python 3.9+ from https://www.python.org/downloads/"
    Write-INFO "Tick 'Add Python to PATH' during installation."
    exit 1
}

# 2. Python packages
Write-STEP "Installing Python packages..."
$reqFile = Join-Path $BackendDir "requirements.txt"
& $pythonExe -m pip install -r $reqFile --quiet
if ($LASTEXITCODE -ne 0) {
    Write-MISS "pip install failed."
    exit 1
}
Write-OK "All Python packages installed."

# 3. yt-dlp
Write-STEP "Checking yt-dlp..."
$ytdlpOut = & $pythonExe -m yt_dlp --version 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-OK "yt-dlp $ytdlpOut"
} else {
    Write-MISS "yt-dlp not available after install."
    exit 1
}

# 4. FFmpeg
Write-STEP "Checking FFmpeg..."
$ffmpegExe  = Find-Exe "ffmpeg"
$ffprobeExe = Find-Exe "ffprobe"
$localFfmpeg  = Join-Path $ToolsDir "ffmpeg.exe"
$localFfprobe = Join-Path $ToolsDir "ffprobe.exe"

$localWorks = $false
if ((Test-Path $localFfmpeg) -and (Test-Path $localFfprobe)) {
    try {
        $null = & $localFfmpeg -version 2>&1
        if ($LASTEXITCODE -eq 0) { $localWorks = $true }
    } catch {}
}

if ($localWorks) {
    Write-OK "Local FFmpeg present and working in tools\ffmpeg\"
    $ffmpegExe  = $localFfmpeg
    $ffprobeExe = $localFfprobe
} elseif ($ffmpegExe -and $ffprobeExe) {
    Write-OK "FFmpeg found on PATH: $ffmpegExe"
    Write-OK "FFprobe found on PATH: $ffprobeExe"
} else {
    Write-INFO "FFmpeg not found. Downloading Windows build into tools\ffmpeg\..."
    Write-INFO "(One-time download, approximately 74 MB)"
    New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null

    $releaseApi = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest"
    Write-INFO "Fetching latest release info from GitHub..."
    try {
        $headers = @{ "User-Agent" = "youtube-media-downloader-setup" }
        $release = Invoke-RestMethod -Uri $releaseApi -Headers $headers -TimeoutSec 30
    } catch {
        Write-MISS "Could not reach GitHub API: $_"
        Write-INFO "Manual fix: download from https://github.com/BtbN/FFmpeg-Builds/releases"
        Write-INFO "Extract ffmpeg.exe, ffprobe.exe and dlls into: tools\ffmpeg\"
        exit 1
    }

    $asset = $release.assets | Where-Object {
        $_.name -match "ffmpeg-master-latest-win64-lgpl-shared\.zip$"
    } | Select-Object -First 1

    if (-not $asset) {
        $asset = $release.assets | Where-Object {
            $_.name -match "win64.*shared\.zip$"
        } | Select-Object -First 1
    }

    if (-not $asset) {
        Write-MISS "Could not locate a suitable FFmpeg ZIP in the latest release."
        Write-INFO "Download manually: https://github.com/BtbN/FFmpeg-Builds/releases"
        exit 1
    }

    $zipPath = Join-Path $env:TEMP "ffmpeg_download.zip"
    Write-INFO "Downloading $($asset.name)..."

    $downloaded = $false
    $curlExe = Find-Exe "curl"
    if ($curlExe) {
        & $curlExe -L --retry 3 --retry-delay 2 -o $zipPath $asset.browser_download_url
        if ($LASTEXITCODE -eq 0 -and (Test-Path $zipPath) -and (Get-Item $zipPath).Length -gt 10000000) {
            $downloaded = $true
        }
    }
    if (-not $downloaded) {
        try {
            Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zipPath -TimeoutSec 600
        } catch {
            Write-MISS "Download failed: $_"
            exit 1
        }
    }

    Write-INFO "Extracting binaries and libraries to tools\ffmpeg\..."
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zipArchive = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
    try {
        foreach ($entry in $zipArchive.Entries) {
            $parts = $entry.FullName.Replace('\', '/').Split('/')
            if ($parts -contains "bin") {
                $leaf = $parts[-1]
                if ($leaf -and ($leaf.EndsWith(".exe") -or $leaf.EndsWith(".dll"))) {
                    $targetPath = Join-Path $ToolsDir $leaf
                    [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $targetPath, $true)
                }
            }
        }
    } finally {
        $zipArchive.Dispose()
    }
    Remove-Item $zipPath -Force -ErrorAction SilentlyContinue

    $ffmpegExe  = $localFfmpeg
    $ffprobeExe = $localFfprobe
    Write-OK "FFmpeg downloaded and configured in tools\ffmpeg\"
}

# 5. Verify
Write-STEP "Verifying FFmpeg and FFprobe..."
$ffVer = & $ffmpegExe -version 2>&1 | Select-String "^ffmpeg version" | Select-Object -First 1
Write-OK "ffmpeg: $ffVer"
$fpVer = & $ffprobeExe -version 2>&1 | Select-String "^ffprobe version" | Select-Object -First 1
Write-OK "ffprobe: $fpVer"

# 6. Summary
Write-Host ""
Write-Host "============================================" -ForegroundColor DarkGray
Write-Host "  Setup complete! Start the app:" -ForegroundColor Green
Write-Host ""
Write-Host "  Terminal 1 (Backend):" -ForegroundColor White
Write-Host "    cd backend; python app.py" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Terminal 2 (Frontend):" -ForegroundColor White
Write-Host "    cd frontend; python -m http.server 8080" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Then open in your browser:" -ForegroundColor White
Write-Host "    http://localhost:8080" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor DarkGray
Write-Host ""
