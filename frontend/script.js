// Relative API URLs so requests work directly with the local Flask server.
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

let currentMode = "video";

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

lookupBtn.addEventListener("click", async () => {
  const url = urlInput.value.trim();
  if (!url) {
    setStatus("Paste a YouTube URL first.", true);
    return;
  }

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
    if (data.thumbnail) thumbEl.src = data.thumbnail;
    infoBox.classList.remove("hidden");
    setStatus("Ready.");
  } catch (err) {
    setStatus(
      "Couldn't reach the backend. Is it running on " + (API_BASE || window.location.origin) + "?",
      true
    );
  }
});

downloadBtn.addEventListener("click", async () => {
  const url = urlInput.value.trim();
  if (!url) {
    setStatus("Paste a YouTube URL first.", true);
    return;
  }

  const quality =
    currentMode === "video" ? videoQualitySelect.value : audioQualitySelect.value;

  downloadBtn.disabled = true;
  setStatus(
    currentMode === "video"
      ? "Downloading video... this can take a while for longer videos."
      : "Downloading audio..."
  );

  try {
    const res = await fetch(`${API_BASE}/api/download`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, mode: currentMode, quality }),
    });

    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      setStatus(data.error || "Download failed.", true);
      return;
    }

    // Pull the filename the backend chose, out of the Content-Disposition header.
    const disposition = res.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename="?([^"]+)"?/);
    const filename = match ? match[1] : `download.${currentMode === "video" ? "mp4" : "mp3"}`;

    const blob = await res.blob();
    const objectUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = objectUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(objectUrl);

    setStatus(`Saved: ${filename}`);
  } catch (err) {
    setStatus(
      "Couldn't reach the backend. Is it running on " + (API_BASE || window.location.origin) + "?",
      true
    );
  } finally {
    downloadBtn.disabled = false;
  }
});
