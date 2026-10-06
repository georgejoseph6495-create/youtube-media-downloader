# YouTube Media Downloader (Local)

A lightweight, local-only web application for downloading YouTube video (MP4) and audio (MP3) using [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) and [`FFmpeg`](https://ffmpeg.org/).

Everything runs entirely on **your own machine**. There is no hosted or public backend, no external server processing, and no third-party tracking. Downloaded media files and stream processing are handled directly on your local system.

---

## Architecture & How It Works

Flask serves both the web frontend and the backend API from a single server on port `5000`. No separate frontend server (such as Python's HTTP server or port 8080) is required.

```text
Your Browser
     │
     ▼
Flask Application (http://127.0.0.1:5000)
     │
     ├── Serves Frontend UI (HTML / CSS / JavaScript)
     ├── API Endpoints (/api/info, /api/download, /api/download/start, etc.)
     │
     ├── yt-dlp
     │     └── Fetches media streams from supported websites with progress hooks
     │
     └── FFmpeg / FFprobe (System PATH or project-local tools/ffmpeg/)
           ├── Merges video and audio streams into MP4
           └── Converts audio streams into MP3
```

- When you start the Flask application, navigate to **`http://127.0.0.1:5000`** in your browser.
- The browser communicates with Flask via relative endpoints (`/api/...`).
- Real-time download progress (percentage, download speed, ETA, and size) is polled while the media is being processed.
- Finished downloads stream directly to your browser's native download manager without buffering huge blobs in JavaScript memory.
- Temporary files and directories are automatically cleaned up after streaming.

---

## Supported Output Formats & Quality Behavior

- **Video (MP4)**: Merges the best available video stream and audio stream into a standard MP4 file.
  - **Quality Options**: Up to 360p, Up to 480p, Up to 720p (Default), Up to 1080p (HD).
  - *Quality Behavior*: Quality selection specifies the **maximum target resolution** (using `yt-dlp`'s `height<=N` filter). The application selects the best stream available up to your chosen resolution; if the source video was uploaded in a lower resolution, it picks the highest available without failing or upscaling.
- **Audio (MP3)**: Extracts audio from the video and converts it into MP3 using FFmpeg.
  - **Bitrate Options**: 128 kbps, 192 kbps (Default), 320 kbps (High Quality).

> 💡 **Note**: Powered by `yt-dlp`, the downloader may also support video downloads from other websites compatible with yt-dlp. Support varies by website.

---

## Project Structure

```text
youtube-media-downloader/
├── backend/
│   ├── app.py              # Flask server: serves frontend & REST API with progress tracking
│   ├── requirements.txt    # Python dependencies (Flask, Flask-CORS, yt-dlp)
│   └── downloads/          # Temporary download working directory (.gitkeep tracked)
├── frontend/
│   ├── index.html          # Web UI interface
│   ├── style.css           # Styling, progress bar & responsive design
│   └── script.js           # Frontend interactivity, progress polling & direct download client
├── tools/
│   └── ffmpeg/             # Directory for project-local FFmpeg binaries (.gitkeep tracked)
├── .gitignore              # Excludes media, caches, venvs, and FFmpeg binaries
├── LICENSE                 # MIT License (Copyright (c) 2026 George Joseph)
├── README.md               # Project documentation
└── setup.ps1               # Automated first-time setup script for Windows
```

---

## Windows Quick Start

To clone and run the application locally on Windows:

```powershell
git clone https://github.com/georgejoseph6495-create/youtube-media-downloader.git
cd youtube-media-downloader
.\setup.ps1
cd backend
python app.py
```

Then open your browser and navigate to:

```text
http://127.0.0.1:5000
```

> **Note:** Only this single Flask server is needed. Do **not** run `python -m http.server 8080` or any second frontend server.

---

## What `setup.ps1` Does

The included `setup.ps1` script automates first-time setup for Windows environments:

1. **Python Check**: Verifies that Python 3.9+ is installed and available.
2. **Dependency Installation**: Runs `pip install -r backend/requirements.txt` to install Flask, Flask-CORS, and `yt-dlp`.
3. **yt-dlp Verification**: Confirms that `yt-dlp` is ready to use.
4. **FFmpeg Detection & Setup**:
   - First checks if `ffmpeg` and `ffprobe` are already available on your system `PATH`.
   - If not found on `PATH` or locally, it automatically downloads a Windows FFmpeg build from the BtbN FFmpeg Builds project directly into `tools/ffmpeg/`.
   - Validates that the FFmpeg binaries are functional.

---

## FFmpeg & Repository Handling

FFmpeg is essential for merging high-quality video and audio streams as well as converting audio to MP3.

- **Binaries Excluded from Git**: FFmpeg executables (`ffmpeg.exe`, `ffprobe.exe`) and associated DLLs are large binary files. They are strictly excluded by `.gitignore` and are **never** committed to the repository.
- **Project-Local FFmpeg**: The `tools/ffmpeg/` directory contains a `.gitkeep` file so the folder structure exists in Git. Running `.\setup.ps1` downloads and extracts project-local FFmpeg binaries when needed without polluting Git history.
- **Fallback**: If you already have FFmpeg installed globally on your machine, `app.py` automatically detects and uses your system's FFmpeg installation.

---

## Third-Party Components

This project relies on the following open-source tools:

- [**yt-dlp**](https://github.com/yt-dlp/yt-dlp): Used to extract metadata and download media from supported websites.
- [**FFmpeg**](https://ffmpeg.org/): Used for multimedia stream merging and audio transcoding.

---

## Responsible Use & Disclaimer

This software is provided for personal and educational purposes only.

- Please respect copyright laws and the intellectual property rights of content creators.
- Only download media that you own, have express permission to download, or that is licensed under a Creative Commons or public domain license.
- Comply with YouTube's Terms of Service when using this tool.
- The author does not condone unauthorized copying or distribution of copyrighted material.

---

## License

This project is licensed under the [MIT License](LICENSE).

Copyright (c) 2026 George Joseph
