"""
YouTube Media Downloader — local backend.

Runs a small Flask API on your own machine that uses yt-dlp (and FFmpeg
under the hood) to fetch video/audio and hand the finished file back to
your browser. Nothing is uploaded anywhere or served publicly.
"""

import os
import re
import shutil
import uuid

from flask import Flask, after_this_request, jsonify, request, send_file, send_from_directory
from flask_cors import CORS
import yt_dlp

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(os.path.dirname(BASE_DIR), "frontend")
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="")
CORS(app, expose_headers=["Content-Disposition"])  # allow the local frontend to read Content-Disposition

WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


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


# ── FFmpeg auto-detection ────────────────────────────────────────────────────
# Priority 1: system PATH   (user already has FFmpeg installed)
# Priority 2: project-local tools/ffmpeg/  (downloaded by setup.ps1)
# If neither is found the app still starts, but the download endpoint will
# return a clear error rather than a raw Python traceback.

def _find_ffmpeg() -> str | None:
    """Return the directory containing ffmpeg[.exe] and ffprobe[.exe], or None."""
    # 1. System PATH
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return None  # None means "let yt-dlp find it on PATH itself"

    # 2. Project-local tools/ffmpeg/
    project_root = os.path.dirname(BASE_DIR)  # one level above backend/
    local_dir = os.path.join(project_root, "tools", "ffmpeg")
    local_ff  = os.path.join(local_dir, "ffmpeg.exe")
    local_fp  = os.path.join(local_dir, "ffprobe.exe")
    if os.path.isfile(local_ff) and os.path.isfile(local_fp):
        return local_dir

    return "NOT_FOUND"


FFMPEG_LOCATION = _find_ffmpeg()  # None | path-string | "NOT_FOUND"


# ── Startup requirement check ────────────────────────────────────────────────

print("\nChecking requirements...")

# Python — always available if this code is running
print("  [OK]  Python")

# yt-dlp
try:
    import importlib.metadata as _meta
    _ytdlp_ver = _meta.version("yt-dlp")
    print(f"  [OK]  yt-dlp {_ytdlp_ver}")
except Exception:
    print("  [!!]  yt-dlp not found — run: pip install yt-dlp")

# FFmpeg / FFprobe
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
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(url, download=False)
        return jsonify(
            {
                "title": info.get("title"),
                "duration": info.get("duration"),
                "thumbnail": info.get("thumbnail"),
                "uploader": info.get("uploader"),
            }
        )
    except Exception as exc:  # noqa: BLE001 - surface the real error to the UI
        import traceback, sys
        try:
            traceback.print_exc()
        except OSError:
            # Python 3.14 + Werkzeug reloader: stderr may be an unwritable pipe
            print(f"[get_info] error: {exc}", file=sys.stderr, flush=False)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/download", methods=["POST"])
def download():
    """
    Download a video or audio file and return it to the browser.

    Expected JSON body:
      { "url": "...", "mode": "video" | "audio", "quality": "720" | "192" }
    """
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    mode = data.get("mode")
    quality = str(data.get("quality") or "").strip()

    if not url or mode not in ("video", "audio"):
        return jsonify({"error": "Invalid request: need 'url' and mode 'video' or 'audio'"}), 400

    # Each request gets its own scratch folder so concurrent downloads
    # never collide, and cleanup is a single rmtree.
    job_id = str(uuid.uuid4())
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    # Guard: surface a friendly error if FFmpeg is completely missing.
    if FFMPEG_LOCATION == "NOT_FOUND":
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify({
            "error": (
                "FFmpeg not found. "
                "Run setup.ps1 from the project root to install it automatically, "
                "or install FFmpeg manually and make sure it is on your PATH."
            )
        }), 500

    try:
        # Pre-extract video metadata to safely sanitize title for Windows filesystem
        safe_title = "download"
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True}) as ydl_info:
                info = ydl_info.extract_info(url, download=False)
                if info and info.get("title"):
                    safe_title = sanitize_filename(info.get("title"))
        except Exception:
            pass

        out_template = os.path.join(job_dir, f"{safe_title}.%(ext)s")

        # Network reliability settings shared by both modes.
        # These map directly to yt-dlp's own params (verified against 2026.8.19).
        # All values are finite — no infinite retry loops.
        _network_opts = {
            "retries": 10,           # HTTP-level retries per fragment/request
            "fragment_retries": 10,  # retries for individual DASH/HLS fragments
            "file_access_retries": 3,  # retries for local file-access operations
            "socket_timeout": 30,    # seconds before a stalled socket is closed
            "extractor_retries": 3,  # retries for extractor-level errors
        }

        if mode == "video":
            height = quality or "720"
            options = {
                "format": f"bestvideo[height<={height}]+bestaudio/best[height<={height}]",
                "merge_output_format": "mp4",
                "outtmpl": out_template,
                "noplaylist": True,
                "quiet": True,
                "windowsfilenames": True,
                **_network_opts,
            }
        else:
            bitrate = quality or "192"
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
                        "preferredquality": bitrate,
                    }
                ],
                **_network_opts,
            }

        # Inject project-local FFmpeg path when yt-dlp cannot find it on PATH.
        if FFMPEG_LOCATION is not None:
            options["ffmpeg_location"] = FFMPEG_LOCATION

        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])

        target_ext = ".mp4" if mode == "video" else ".mp3"
        files = [f for f in os.listdir(job_dir) if f.endswith(target_ext)]
        if not files:
            files = [f for f in os.listdir(job_dir) if not f.endswith((".part", ".ytdl"))]
        if not files:
            files = os.listdir(job_dir)
        if not files:
            shutil.rmtree(job_dir, ignore_errors=True)
            return jsonify({"error": "Download finished but no file was produced"}), 500

        filepath = os.path.join(job_dir, files[0])
        filename = files[0]

        response = send_file(filepath, as_attachment=True, download_name=filename)

        @response.call_on_close
        def cleanup_after_stream():
            # Remove the temp copy once streaming finishes and file handle closes
            try:
                if hasattr(response.response, "file") and hasattr(response.response.file, "close"):
                    response.response.file.close()
                if hasattr(response.response, "close"):
                    response.response.close()
                shutil.rmtree(job_dir, ignore_errors=True)
            except Exception:
                pass

        @after_this_request
        def cleanup(resp):
            return resp

        return response

    except Exception as exc:  # noqa: BLE001 - surface real yt-dlp/ffmpeg errors
        import traceback, sys
        try:
            traceback.print_exc()
        except OSError:
            # Python 3.14 + Werkzeug reloader: stderr may be an unwritable pipe
            print(f"[download] error: {exc}", file=sys.stderr, flush=False)
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
