"""
YouTube Media Downloader — local backend.

Runs a small Flask API on your own machine that uses yt-dlp (and FFmpeg
under the hood) to fetch video/audio and hand the finished file back to
your browser. Nothing is uploaded anywhere or served publicly.
"""

import os
import re
import shutil
import threading
import time
import uuid

from flask import Flask, jsonify, request, send_file, send_from_directory
from flask_cors import CORS
import yt_dlp

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(os.path.dirname(BASE_DIR), "frontend")
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="")
CORS(app, expose_headers=["Content-Disposition"])  # allow local frontend to read Content-Disposition

WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}

# In-memory progress tracking for asynchronous browser downloads
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def clean_error_message(err: str) -> str:
    """Strip ANSI escape codes and redundant prefixes for clean user display."""
    msg = str(err)
    # Strip ANSI terminal escapes
    msg = re.sub(r'\x1b\[[0-9;]*[mGKH]', '', msg)
    # Strip yt-dlp ERROR prefixes
    msg = re.sub(r'^ERROR:\s*(\[[^\]]+\]\s*)?', '', msg).strip()
    return msg or "An unexpected error occurred."


def format_bytes(bytes_count: int | float | None) -> str:
    """Format byte counts into human-readable strings (e.g. 14.2 MB)."""
    if not bytes_count or bytes_count <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB"]
    val = float(bytes_count)
    i = 0
    while val >= 1024.0 and i < len(units) - 1:
        val /= 1024.0
        i += 1
    return f"{val:.1f} {units[i]}"


def safe_rmtree(path: str, retries: int = 5, delay: float = 0.2) -> None:
    """Safely delete a directory tree with retries to handle brief Windows file locks."""
    for _ in range(retries):
        try:
            shutil.rmtree(path, ignore_errors=False)
            return
        except Exception:
            time.sleep(delay)
    shutil.rmtree(path, ignore_errors=True)


def cleanup_stale_downloads(max_age_seconds: int = 1800, force_all: bool = False) -> None:
    """
    Purge stale temporary download directories from downloads/ folder.
    Preserves .gitkeep file so directory tracking remains intact in Git.
    Guarantees active download job directories are NEVER deleted by stale cleanup.
    """
    now = time.time()
    active_job_ids = set()
    with JOBS_LOCK:
        for j_id, j_data in JOBS.items():
            status = j_data.get("status")
            if status in ("starting", "downloading", "processing"):
                active_job_ids.add(j_id)
            elif status == "finished":
                # Keep recently finished jobs for up to 10 minutes so users can download
                created = j_data.get("created_at", now)
                if now - created < 600:
                    active_job_ids.add(j_id)

    try:
        for item in os.listdir(DOWNLOAD_DIR):
            if item == ".gitkeep":
                continue
            # When cleaning up stale files during runtime, NEVER delete active jobs
            if not force_all and item in active_job_ids:
                continue
            item_path = os.path.join(DOWNLOAD_DIR, item)
            if os.path.isdir(item_path):
                try:
                    mtime = os.path.getmtime(item_path)
                    if force_all or (now - mtime > max_age_seconds):
                        safe_rmtree(item_path)
                        with JOBS_LOCK:
                            JOBS.pop(item, None)
                except Exception:
                    pass
    except Exception:
        pass


def sanitize_filename(name: str, max_length: int = 120) -> str:
    """
    Sanitize a title/filename for safe use on Windows and across filesystems.
    Preserves readable YouTube titles while stripping or replacing illegal characters.
    """
    if not name:
        return "download"

    name = str(name).strip()
    name = name.replace(":", " -")
    name = name.replace("/", "_").replace("\\", "_")
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = "".join(ch for ch in name if ord(ch) >= 32 and ch != '\x7f')
    name = re.sub(r'[\s]+', ' ', name)
    name = re.sub(r'_+', '_', name)
    name = name.strip(' ._')

    if len(name) > max_length:
        name = name[:max_length].rstrip(' ._')

    base = name.split('.')[0].upper()
    if base in WINDOWS_RESERVED or not name:
        name = f"_{name}" if name else "download"

    return name or "download"


def validate_url(url: str) -> tuple[bool, str]:
    """Validate user URL input before passing to yt-dlp."""
    if not url:
        return False, "No URL provided."
    if not (url.startswith("http://") or url.startswith("https://")):
        return False, "Please enter a valid web URL starting with http:// or https://"
    return True, ""


def validate_mode_and_quality(mode: str, quality: str) -> tuple[str, str, str]:
    """Validate mode and sanitize quality/bitrate parameters."""
    if mode not in ("video", "audio"):
        return "", "", "Invalid mode: must be 'video' or 'audio'."
    if mode == "video":
        height = re.sub(r'\D', '', str(quality or ""))
        if not height or not (144 <= int(height) <= 4320):
            height = "720"
        return mode, height, ""
    else:
        bitrate = re.sub(r'\D', '', str(quality or ""))
        if not bitrate or not (64 <= int(bitrate) <= 320):
            bitrate = "192"
        return mode, bitrate, ""


# ── FFmpeg auto-detection ────────────────────────────────────────────────────
# Priority 1: system PATH   (user already has FFmpeg installed)
# Priority 2: project-local tools/ffmpeg/  (downloaded by setup.ps1)

def _find_ffmpeg() -> str | None:
    """Return the directory containing ffmpeg and ffprobe, or None."""
    # 1. System PATH
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return None  # None means "let yt-dlp find it on PATH itself"

    # 2. Project-local tools/ffmpeg/
    project_root = os.path.dirname(BASE_DIR)
    local_dir = os.path.join(project_root, "tools", "ffmpeg")
    has_ffmpeg = os.path.isfile(os.path.join(local_dir, "ffmpeg.exe")) or os.path.isfile(os.path.join(local_dir, "ffmpeg"))
    has_ffprobe = os.path.isfile(os.path.join(local_dir, "ffprobe.exe")) or os.path.isfile(os.path.join(local_dir, "ffprobe"))
    if has_ffmpeg and has_ffprobe:
        return local_dir

    return "NOT_FOUND"


FFMPEG_LOCATION = _find_ffmpeg()


def get_ffmpeg_location() -> str | None:
    """Dynamically get or re-check FFmpeg location if previously not found."""
    global FFMPEG_LOCATION
    if FFMPEG_LOCATION == "NOT_FOUND":
        FFMPEG_LOCATION = _find_ffmpeg()
    return FFMPEG_LOCATION


# ── Startup requirement check ────────────────────────────────────────────────

print("\nChecking requirements...")
print("  [OK]  Python")

try:
    import importlib.metadata as _meta
    _ytdlp_ver = _meta.version("yt-dlp")
    print(f"  [OK]  yt-dlp {_ytdlp_ver}")
except Exception:
    print("  [!!]  yt-dlp not found — run: pip install yt-dlp")

if FFMPEG_LOCATION == "NOT_FOUND":
    print("  [!!]  FFmpeg not found")
    print("  [!!]  FFprobe not found")
    print("        Run setup.ps1 to install the required components.")
elif FFMPEG_LOCATION is None:
    print("  [OK]  FFmpeg  (system PATH)")
    print("  [OK]  FFprobe (system PATH)")
else:
    print(f"  [OK]  FFmpeg  (project-local: {FFMPEG_LOCATION})")
    print(f"  [OK]  FFprobe (project-local: {FFMPEG_LOCATION})")

if FFMPEG_LOCATION == "NOT_FOUND":
    print("\n  WARNING: audio extraction and video merging will fail until")
    print("  FFmpeg is installed. Run .\\setup.ps1 from the project root.\n")
else:
    print("\n  All requirements satisfied.\n")

# Clean leftover temporary download folders on server startup
cleanup_stale_downloads(force_all=True)


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/api/info", methods=["POST"])
def get_info():
    """Look up basic metadata for a URL before downloading."""
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    is_valid, err_msg = validate_url(url)
    if not is_valid:
        return jsonify({"error": err_msg}), 400

    try:
        # Pass socket_timeout and retries to avoid hanging on slow network
        ydl_opts = {
            "quiet": True,
            "noplaylist": True,
            "socket_timeout": 15,
            "retries": 3,
            "extractor_retries": 3,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return jsonify(
            {
                "title": info.get("title"),
                "duration": info.get("duration"),
                "thumbnail": info.get("thumbnail"),
                "uploader": info.get("uploader"),
            }
        )
    except yt_dlp.utils.DownloadError as exc:
        clean_msg = clean_error_message(str(exc))
        return jsonify({"error": clean_msg}), 400
    except Exception as exc:
        clean_msg = clean_error_message(str(exc))
        return jsonify({"error": clean_msg}), 500


def _download_worker(job_id: str, url: str, mode: str, quality: str) -> None:
    """Background worker for asynchronous download with progress updates."""
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    ffmpeg_loc = get_ffmpeg_location()
    if ffmpeg_loc == "NOT_FOUND":
        safe_rmtree(job_dir)
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["status"] = "error"
                JOBS[job_id]["error"] = (
                    "FFmpeg not found. Run setup.ps1 from the project root to install it automatically, "
                    "or install FFmpeg manually and make sure it is on your PATH."
                )
        return

    try:
        safe_title = "download"
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True, "socket_timeout": 15}) as ydl_info:
                info = ydl_info.extract_info(url, download=False)
                if info and info.get("title"):
                    safe_title = sanitize_filename(info.get("title"))
        except Exception:
            pass

        out_template = os.path.join(job_dir, f"{safe_title}.%(ext)s")

        def _progress_hook(d):
            status = d.get("status")
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                if not job:
                    return
                if status == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    downloaded = d.get("downloaded_bytes") or 0
                    percent = round((downloaded / total * 100), 1) if total else 0.0
                    speed = d.get("speed")
                    eta = d.get("eta")
                    job["status"] = "downloading"
                    job["percent"] = min(percent, 99.9)
                    job["downloaded"] = format_bytes(downloaded)
                    job["total"] = format_bytes(total) if total else "Unknown"
                    job["speed"] = f"{format_bytes(speed)}/s" if speed else ""
                    job["eta"] = f"{int(eta)}s" if eta and eta > 0 else ""
                    job["phase"] = f"Downloading media ({job['percent']}%)"
                elif status == "finished":
                    job["status"] = "processing"
                    job["percent"] = 99.0
                    job["phase"] = "Processing / Merging media..."

        def _postprocessor_hook(d):
            status = d.get("status")
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                if not job:
                    return
                if status == "started":
                    job["status"] = "processing"
                    job["phase"] = "Converting / Merging with FFmpeg..."
                elif status == "finished":
                    job["phase"] = "Finalizing output file..."

        _network_opts = {
            "retries": 10,
            "fragment_retries": 10,
            "file_access_retries": 3,
            "socket_timeout": 30,
            "extractor_retries": 3,
        }

        if mode == "video":
            options = {
                "format": f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]",
                "merge_output_format": "mp4",
                "outtmpl": out_template,
                "noplaylist": True,
                "quiet": True,
                "windowsfilenames": True,
                "progress_hooks": [_progress_hook],
                "postprocessor_hooks": [_postprocessor_hook],
                **_network_opts,
            }
        else:
            options = {
                "format": "bestaudio/best",
                "outtmpl": out_template,
                "noplaylist": True,
                "quiet": True,
                "windowsfilenames": True,
                "progress_hooks": [_progress_hook],
                "postprocessor_hooks": [_postprocessor_hook],
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": quality,
                    }
                ],
                **_network_opts,
            }

        if ffmpeg_loc is not None:
            options["ffmpeg_location"] = ffmpeg_loc

        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])

        target_ext = ".mp4" if mode == "video" else ".mp3"
        expected_format = "MP4" if mode == "video" else "MP3"

        # Strictly select files matching the expected extension, ignoring temporary download fragments
        files = [
            f for f in os.listdir(job_dir)
            if f.lower().endswith(target_ext) and not f.endswith((".part", ".ytdl"))
        ]
        if not files:
            safe_rmtree(job_dir)
            with JOBS_LOCK:
                if job_id in JOBS:
                    job = JOBS[job_id]
                    job["status"] = "error"
                    job["error"] = f"Download finished but no valid {expected_format} file was produced."
                    job["phase"] = "Failed"
            return

        filepath = os.path.join(job_dir, files[0])
        filename = files[0]

        with JOBS_LOCK:
            if job_id in JOBS:
                job = JOBS[job_id]
                job["status"] = "finished"
                job["percent"] = 100.0
                job["phase"] = "Complete! Ready to download."
                job["filepath"] = filepath
                job["filename"] = filename

        # Fallback cleanup: if file is never downloaded, purge after 10 minutes
        def _abandoned_purge():
            time.sleep(600)
            safe_rmtree(job_dir)
            with JOBS_LOCK:
                JOBS.pop(job_id, None)

        threading.Thread(target=_abandoned_purge, daemon=True).start()

    except Exception as exc:
        safe_rmtree(job_dir)
        with JOBS_LOCK:
            if job_id in JOBS:
                job = JOBS[job_id]
                job["status"] = "error"
                job["error"] = clean_error_message(str(exc))
                job["phase"] = "Failed"


@app.route("/api/download/start", methods=["POST"])
def start_download():
    """
    Start an asynchronous download job with real-time progress tracking.
    Returns a job_id for polling progress.
    """
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    mode = data.get("mode")
    raw_quality = data.get("quality")

    is_valid, err_msg = validate_url(url)
    if not is_valid:
        return jsonify({"error": err_msg}), 400

    mode, quality, mode_err = validate_mode_and_quality(mode, raw_quality)
    if mode_err:
        return jsonify({"error": mode_err}), 400

    cleanup_stale_downloads(max_age_seconds=1800)

    job_id = str(uuid.uuid4())
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)

    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "starting",
            "percent": 0.0,
            "speed": "",
            "eta": "",
            "downloaded": "",
            "total": "",
            "phase": "Connecting to YouTube...",
            "error": None,
            "filepath": None,
            "filename": None,
            "created_at": time.time(),
            "job_dir": job_dir,
        }

    threading.Thread(
        target=_download_worker,
        args=(job_id, url, mode, quality),
        daemon=True,
    ).start()

    return jsonify({"job_id": job_id})


@app.route("/api/download/progress/<job_id>", methods=["GET"])
def get_progress(job_id: str):
    """Poll progress for an ongoing download job."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404

        return jsonify({
            "status": job["status"],
            "percent": job["percent"],
            "speed": job["speed"],
            "eta": job["eta"],
            "downloaded": job["downloaded"],
            "total": job["total"],
            "phase": job["phase"],
            "filename": job["filename"],
            "error": job["error"],
        })


@app.route("/api/download/file/<job_id>", methods=["GET"])
def get_downloaded_file(job_id: str):
    """
    Stream finished file directly to browser without loading it into JavaScript memory.
    Cleans up temporary directory once the stream closes.
    """
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Job not found or expired"}), 404
        if job["status"] != "finished" or not job["filepath"]:
            return jsonify({"error": "File is not ready yet"}), 400

        filepath = job["filepath"]
        filename = job["filename"]
        job_dir = job["job_dir"]

    # Security check: ensure filepath is inside DOWNLOAD_DIR
    real_path = os.path.realpath(filepath)
    real_down_dir = os.path.realpath(DOWNLOAD_DIR)
    if not real_path.startswith(real_down_dir):
        return jsonify({"error": "Access denied"}), 403

    if not os.path.isfile(filepath):
        return jsonify({"error": "File no longer exists"}), 404

    response = send_file(filepath, as_attachment=True, download_name=filename)

    @response.call_on_close
    def cleanup_after_stream():
        def _delayed_clean():
            try:
                time.sleep(1.0)
                safe_rmtree(job_dir)
                with JOBS_LOCK:
                    JOBS.pop(job_id, None)
            except Exception:
                pass
        threading.Thread(target=_delayed_clean, daemon=True).start()

    return response


@app.route("/api/download", methods=["POST"])
def download():
    """
    Synchronous download endpoint (preserves backwards compatibility for direct API calls).
    """
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    mode = data.get("mode")
    raw_quality = data.get("quality")

    is_valid, err_msg = validate_url(url)
    if not is_valid:
        return jsonify({"error": err_msg}), 400

    mode, quality, mode_err = validate_mode_and_quality(mode, raw_quality)
    if mode_err:
        return jsonify({"error": mode_err}), 400

    job_id = str(uuid.uuid4())
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "downloading",
            "job_dir": job_dir,
            "created_at": time.time(),
        }

    ffmpeg_loc = get_ffmpeg_location()
    if ffmpeg_loc == "NOT_FOUND":
        safe_rmtree(job_dir)
        with JOBS_LOCK:
            JOBS.pop(job_id, None)
        return jsonify({
            "error": (
                "FFmpeg not found. "
                "Run setup.ps1 from the project root to install it automatically, "
                "or install FFmpeg manually and make sure it is on your PATH."
            )
        }), 500

    try:
        safe_title = "download"
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True, "socket_timeout": 15}) as ydl_info:
                info = ydl_info.extract_info(url, download=False)
                if info and info.get("title"):
                    safe_title = sanitize_filename(info.get("title"))
        except Exception:
            pass

        out_template = os.path.join(job_dir, f"{safe_title}.%(ext)s")

        _network_opts = {
            "retries": 10,
            "fragment_retries": 10,
            "file_access_retries": 3,
            "socket_timeout": 30,
            "extractor_retries": 3,
        }

        if mode == "video":
            options = {
                "format": f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]",
                "merge_output_format": "mp4",
                "outtmpl": out_template,
                "noplaylist": True,
                "quiet": True,
                "windowsfilenames": True,
                **_network_opts,
            }
        else:
            options = {
                "format": "bestaudio/best",
                "outtmpl": out_template,
                "noplaylist": True,
                "quiet": True,
                "windowsfilenames": True,
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": quality,
                    }
                ],
                **_network_opts,
            }

        if ffmpeg_loc is not None:
            options["ffmpeg_location"] = ffmpeg_loc

        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])

        target_ext = ".mp4" if mode == "video" else ".mp3"
        expected_format = "MP4" if mode == "video" else "MP3"

        # Strictly select files matching the expected extension, ignoring temporary download fragments
        files = [
            f for f in os.listdir(job_dir)
            if f.lower().endswith(target_ext) and not f.endswith((".part", ".ytdl"))
        ]
        if not files:
            safe_rmtree(job_dir)
            with JOBS_LOCK:
                JOBS.pop(job_id, None)
            return jsonify({"error": f"Download finished but no valid {expected_format} file was produced."}), 500

        filepath = os.path.join(job_dir, files[0])
        filename = files[0]

        response = send_file(filepath, as_attachment=True, download_name=filename)

        @response.call_on_close
        def cleanup_after_stream():
            try:
                if hasattr(response.response, "file") and hasattr(response.response.file, "close"):
                    response.response.file.close()
                if hasattr(response.response, "close"):
                    response.response.close()
                safe_rmtree(job_dir)
                with JOBS_LOCK:
                    JOBS.pop(job_id, None)
            except Exception:
                pass

        return response

    except Exception as exc:
        safe_rmtree(job_dir)
        with JOBS_LOCK:
            JOBS.pop(job_id, None)
        clean_msg = clean_error_message(str(exc))
        return jsonify({"error": clean_msg}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
