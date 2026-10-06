// Relative API URLs so requests work directly with the local Flask server on port 5000.
const API_BASE = "";

const urlInput = document.getElementById("url");
const lookupBtn = document.getElementById("lookupBtn");
const downloadBtn = document.getElementById("downloadBtn");
const statusEl = document.getElementById("status");

const infoBox = document.getElementById("info");
const thumbEl = document.getElementById("thumb");
const titleEl = document.getElementById("title");
const metaEl = document.getElementById("meta");

const modeButtons = document.querySelectorAll(".mode-btn");
const videoQualityRow = document.getElementById("videoQuality");
const audioQualityRow = document.getElementById("audioQuality");
const videoQualitySelect = document.getElementById("videoQualitySelect");
const audioQualitySelect = document.getElementById("audioQualitySelect");

const progressContainer = document.getElementById("progressContainer");
const progressBar = document.getElementById("progressBar");
const progressPhase = document.getElementById("progressPhase");
const progressStats = document.getElementById("progressStats");

let currentMode = "video";
let activePollInterval = null;

// Toggle between Video (MP4) and Audio (MP3) modes
modeButtons.forEach((btn) => {
  btn.addEventListener("click", () => {
    modeButtons.forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    currentMode = btn.dataset.mode;
    videoQualityRow.classList.toggle("hidden", currentMode !== "video");
    audioQualityRow.classList.toggle("hidden", currentMode !== "audio");
  });
});

function setStatus(message, isError = false) {
  statusEl.textContent = message;
  statusEl.style.color = isError ? "#ff6b6b" : "#9aa0ac";
}

function formatDuration(seconds) {
  if (!seconds && seconds !== 0) return "";
  const m = Math.floor(seconds / 60);
  const s = String(Math.floor(seconds % 60)).padStart(2, "0");
  return `${m}:${s}`;
}

// Extract filename safely from Content-Disposition header (supports UTF-8 filename*)
function parseFilenameFromHeader(header, fallback) {
  if (!header) return fallback;

  // RFC 5987 / RFC 6266 UTF-8 encoded filename
  const utf8Match = header.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8Match && utf8Match[1]) {
    try {
      return decodeURIComponent(utf8Match[1].trim().replace(/^["']|["']$/g, ""));
    } catch (_) {}
  }

  // Standard ASCII filename
  const asciiMatch = header.match(/filename="?([^";]+)"?/i);
  if (asciiMatch && asciiMatch[1]) {
    return asciiMatch[1].trim();
  }

  return fallback;
}

// Thumbnail fallback on error
thumbEl.addEventListener("error", () => {
  thumbEl.style.display = "none";
});

// Look up video metadata
lookupBtn.addEventListener("click", async () => {
  const url = urlInput.value.trim();
  if (!url) {
    setStatus("Paste a URL first.", true);
    return;
  }
  if (!/^https?:\/\//i.test(url)) {
    setStatus("Please enter a valid URL starting with http:// or https://", true);
    return;
  }

  lookupBtn.disabled = true;
  setStatus("Looking up video info...");
  infoBox.classList.add("hidden");

  try {
    const res = await fetch(`${API_BASE}/api/info`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const data = await res.json();

    if (!res.ok) {
      setStatus(data.error || "Could not fetch video info.", true);
      return;
    }

    titleEl.textContent = data.title || "Untitled";
    metaEl.textContent = [data.uploader, formatDuration(data.duration)]
      .filter(Boolean)
      .join(" • ");

    if (data.thumbnail) {
      thumbEl.src = data.thumbnail;
      thumbEl.style.display = "block";
    } else {
      thumbEl.style.display = "none";
    }

    infoBox.classList.remove("hidden");
    setStatus("Ready to download.");
  } catch (err) {
    setStatus(
      "Couldn't reach the backend. Is it running on " + (API_BASE || window.location.origin) + "?",
      true
    );
  } finally {
    lookupBtn.disabled = false;
  }
});

// Download handler with real progress and low-memory direct streaming
downloadBtn.addEventListener("click", async () => {
  const url = urlInput.value.trim();
  if (!url) {
    setStatus("Paste a URL first.", true);
    return;
  }
  if (!/^https?:\/\//i.test(url)) {
    setStatus("Please enter a valid URL starting with http:// or https://", true);
    return;
  }

  const quality =
    currentMode === "video" ? videoQualitySelect.value : audioQualitySelect.value;

  // Clear previous intervals if any
  if (activePollInterval) {
    clearInterval(activePollInterval);
    activePollInterval = null;
  }

  downloadBtn.disabled = true;
  lookupBtn.disabled = true;

  // Reset progress UI
  progressContainer.classList.remove("hidden");
  progressBar.style.width = "0%";
  progressPhase.textContent = "Connecting to YouTube...";
  progressStats.textContent = "";
  setStatus("Starting download...");

  try {
    // 1. Initiate asynchronous download job with progress tracking
    const startRes = await fetch(`${API_BASE}/api/download/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, mode: currentMode, quality }),
    });

    if (!startRes.ok) {
      // If start endpoint rejected input (e.g. invalid URL)
      const errData = await startRes.json().catch(() => ({}));
      throw new Error(errData.error || `Download failed with status ${startRes.status}`);
    }

    const { job_id } = await startRes.json();
    if (!job_id) {
      throw new Error("Server did not return a valid download job ID.");
    }

    // 2. Poll progress endpoint
    activePollInterval = setInterval(async () => {
      try {
        const progRes = await fetch(`${API_BASE}/api/download/progress/${job_id}`);
        if (!progRes.ok) {
          clearInterval(activePollInterval);
          activePollInterval = null;
          progressContainer.classList.add("hidden");
          setStatus("Download job lost or expired.", true);
          downloadBtn.disabled = false;
          lookupBtn.disabled = false;
          return;
        }

        const job = await progRes.json();

        // Update progress bar and phase
        const pct = Math.max(0, Math.min(100, job.percent || 0));
        progressBar.style.width = `${pct}%`;
        progressPhase.textContent = job.phase || "Downloading...";

        // Format stats string: e.g. "12.4 MB / 24.8 MB • 3.2 MB/s • ETA: 4s"
        const statsParts = [];
        if (job.downloaded) {
          statsParts.push(job.total && job.total !== "Unknown" ? `${job.downloaded} / ${job.total}` : job.downloaded);
        }
        if (job.speed) statsParts.push(job.speed);
        if (job.eta) statsParts.push(`ETA: ${job.eta}`);
        progressStats.textContent = statsParts.join(" • ");

        if (job.status === "finished") {
          clearInterval(activePollInterval);
          activePollInterval = null;
          progressBar.style.width = "100%";
          progressPhase.textContent = "Complete!";
          progressStats.textContent = "";

          // 3. Direct browser streaming download (avoids buffering entire blob into JS heap)
          const downloadUrl = `${API_BASE}/api/download/file/${job_id}`;
          const downloadLink = document.createElement("a");
          downloadLink.href = downloadUrl;
          downloadLink.download = job.filename || `download.${currentMode === "video" ? "mp4" : "mp3"}`;
          downloadLink.style.display = "none";
          document.body.appendChild(downloadLink);
          downloadLink.click();
          setTimeout(() => downloadLink.remove(), 1000);

          setStatus(`Saved: ${job.filename || "file"}`);
          downloadBtn.disabled = false;
          lookupBtn.disabled = false;
        } else if (job.status === "error") {
          clearInterval(activePollInterval);
          activePollInterval = null;
          progressContainer.classList.add("hidden");
          setStatus(job.error || "Download failed.", true);
          downloadBtn.disabled = false;
          lookupBtn.disabled = false;
        }
      } catch (pollErr) {
        // Network flicker during poll - keep polling up to normal errors
      }
    }, 600);

  } catch (err) {
    if (activePollInterval) {
      clearInterval(activePollInterval);
      activePollInterval = null;
    }
    progressContainer.classList.add("hidden");
    setStatus(err.message || "Couldn't reach the backend. Is it running on " + (API_BASE || window.location.origin) + "?", true);
    downloadBtn.disabled = false;
    lookupBtn.disabled = false;
  }
});
