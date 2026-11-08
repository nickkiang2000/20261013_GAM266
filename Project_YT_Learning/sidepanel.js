const $ = (id) => document.getElementById(id);
const statusText = $("statusText");
const statusDot = $("statusDot");
const captionList = $("captionList");
const emptyState = $("emptyState");

let activeTabId = null;
let currentVideo = { videoId: "", title: "", url: "" };
let sessions = {};
let activeSession = null;

function blankSession(video) {
  return {
    video: video || { videoId: "", title: "", url: "" },
    captionTrack: {
      language: $("languageInput").value.trim() || null,
      sourceType: "mixed",
      sourceMethod: "visible_dom",
      completeness: "partial"
    },
    segments: [],
    updatedAt: new Date().toISOString()
  };
}

function sessionKey(videoId) {
  return videoId || "__no_video__";
}

function getSession() {
  const key = sessionKey(currentVideo.videoId);
  if (!sessions[key]) sessions[key] = blankSession(currentVideo);
  sessions[key].video = { ...sessions[key].video, ...currentVideo };
  return sessions[key];
}

async function persist() {
  await chrome.storage.local.set({ ytKnowledgeSessions: sessions });
}

async function loadSaved() {
  const saved = await chrome.storage.local.get("ytKnowledgeSessions");
  sessions = saved.ytKnowledgeSessions || {};
}

function setStatus(text, ok = false) {
  statusText.textContent = text;
  statusDot.classList.toggle("ok", ok);
}

function formatTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0
    ? `${String(h).padStart(2,"0")}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`
    : `${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`;
}

function render() {
  const session = getSession();
  const segments = session.segments || [];
  $("videoId").textContent = currentVideo.videoId || "—";
  $("videoTitle").textContent = currentVideo.title || "—";
  $("count").textContent = String(segments.length);
  $("shownCount").textContent = `${segments.length} 筆`;
  captionList.replaceChildren();
  emptyState.hidden = segments.length > 0;

  for (const item of segments.slice(-300)) {
    const li = document.createElement("li");
    const time = document.createElement("button");
    time.className = "time";
    time.type = "button";
    time.textContent = item.timestamp || formatTime(item.start);
    time.title = "跳至字幕時間";
    time.addEventListener("click", async () => {
      if (activeTabId && item.sourceMethod === "visible_dom") {
        await chrome.tabs.sendMessage(activeTabId, {
          type: "YT_CAPTION_SEEK", seconds: item.start
        }).catch(() => {});
      } else if (currentVideo.url) {
        const url = new URL(currentVideo.url);
        if (Number.isFinite(item.start)) url.searchParams.set("t", `${Math.floor(item.start)}s`);
        await chrome.tabs.update(activeTabId, { url: url.toString() }).catch(() => {});
      }
    });
    const text = document.createElement("span");
    text.className = "caption-text";
    text.textContent = item.text;
    li.append(time, text);
    captionList.append(li);
  }
}

function normalizeText(text) {
  return (text || "").replace(/\s+/g, " ").trim();
}

function parseTimestamp(value) {
  const match = value.trim().match(/(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[,.](\d{1,3})/);
  if (!match) return null;
  const hours = Number(match[1] || 0);
  const minutes = Number(match[2]);
  const seconds = Number(match[3]);
  const millis = Number((match[4] || "0").padEnd(3, "0").slice(0, 3));
  return hours * 3600 + minutes * 60 + seconds + millis / 1000;
}

function parseSubtitleFile(raw, filename = "") {
  const content = raw.replace(/^\uFEFF/, "").replace(/\r/g, "");
  const isVtt = /^WEBVTT/m.test(content);
  const blocks = content.split(/\n\s*\n/);
  const out = [];

  for (const block of blocks) {
    const lines = block.split("\n").map(x => x.trim()).filter(Boolean);
    const timingIndex = lines.findIndex(line => line.includes("-->"));
    if (timingIndex >= 0) {
      const timing = lines[timingIndex].split("-->");
      const start = parseTimestamp(timing[0]);
      const end = timing[1] ? parseTimestamp(timing[1].trim().split(/\s+/)[0]) : null;
      const text = normalizeText(lines.slice(timingIndex + 1)
        .join(" ").replace(/<[^>]*>/g, "").replace(/&nbsp;/g, " "));
      if (text && start !== null) {
        out.push({
          text, start, end,
          duration: end !== null ? Math.max(0, end - start) : null,
          timestamp: formatTime(start)
        });
      }
      continue;
    }
  }

  if (out.length) return { segments: out, format: isVtt ? "vtt" : "srt" };

  // Plain transcript: accept lines with [00:12] / 00:12 / 01:02:03 prefixes.
  for (const line of content.split("\n")) {
    const match = line.trim().match(/^\[?((?:\d{1,2}:)?\d{1,2}:\d{2}(?:[,.]\d{1,3})?)\]?\s+(.+)$/);
    if (match) {
      const start = parseTimestamp(match[1].replace(",", "."));
      const text = normalizeText(match[2]);
      if (start !== null && text) out.push({
        text, start, end: null, duration: null, timestamp: formatTime(start)
      });
    }
  }
  if (out.length) return { segments: out, format: "timestamped_text" };

  const text = normalizeText(content);
  if (!text) throw new Error("檔案沒有可匯入的文字。");
  return {
    segments: [{ text, start: null, end: null, duration: null, timestamp: "—" }],
    format: "plain_text"
  };
}

function mergeSegments(existing, incoming, sourceMethod, sourceType) {
  const seen = new Set(existing.map(x =>
    `${x.sourceMethod}|${x.start ?? "na"}|${normalizeText(x.text).toLocaleLowerCase()}`
  ));
  const additions = [];
  for (const segment of incoming) {
    const text = normalizeText(segment.text);
    if (!text) continue;
    const key = `${sourceMethod}|${segment.start ?? "na"}|${text.toLocaleLowerCase()}`;
    if (seen.has(key)) continue;
    seen.add(key);
    additions.push({
      id: crypto.randomUUID(),
      text,
      start: segment.start ?? null,
      end: segment.end ?? null,
      duration: segment.duration ?? null,
      timestamp: segment.timestamp || (segment.start != null ? formatTime(segment.start) : "—"),
      sourceMethod,
      sourceType,
      observedAt: new Date().toISOString()
    });
  }
  return additions;
}

async function getActiveYouTubeTab() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const tab = tabs[0];
  if (!tab?.id || !tab.url?.startsWith("https://www.youtube.com/")) {
    activeTabId = null;
    currentVideo = { videoId: "", title: "", url: "" };
    setStatus("請先切換到 YouTube 影片頁面", false);
    render();
    return;
  }
  activeTabId = tab.id;
  try {
    const state = await chrome.tabs.sendMessage(tab.id, { type: "YT_CAPTION_GET_STATE" });
    currentVideo = state.video || {
      videoId: new URL(tab.url).searchParams.get("v") || "",
      title: tab.title || "",
      url: tab.url
    };
    getSession();
    setStatus("已連線；可觀察字幕或匯入逐字稿", true);
    await persist();
    render();
  } catch {
    currentVideo = {
      videoId: new URL(tab.url).searchParams.get("v") || "",
      title: tab.title || "",
      url: tab.url
    };
    setStatus("無法連線；請重新載入 YouTube 分頁", false);
    render();
  }
}

async function importSegments(segments, sourceMethod, sourceType, language) {
  if (!currentVideo.videoId) {
    alert("請先切換到一支 YouTube 影片，再匯入字幕。");
    return;
  }
  const session = getSession();
  const additions = mergeSegments(session.segments, segments, sourceMethod, sourceType);
  session.segments.push(...additions);
  if (language) session.captionTrack.language = language;
  session.captionTrack.sourceMethod = sourceMethod;
  session.captionTrack.sourceType = sourceType;
  session.captionTrack.completeness = sourceMethod === "user_file_import" || sourceMethod === "user_paste"
    ? "user_provided_not_verified" : "partial";
  session.updatedAt = new Date().toISOString();
  await persist();
  render();
  setStatus(`匯入完成：新增 ${additions.length} 段`, true);
}

chrome.runtime.onMessage.addListener(async (message, sender) => {
  if (!message) return;
  if (message.type === "YT_CAPTION_RESET" && sender.tab?.id === activeTabId) {
    const previousId = currentVideo.videoId;
    if (previousId) await persist();
    currentVideo = message.video || { videoId: "", title: "", url: "" };
    getSession();
    await persist();
    setStatus("影片已切換；舊影片資料已保留", true);
    render();
  }
  if (message.type === "YT_CAPTION_UPDATE" && sender.tab?.id === activeTabId) {
    currentVideo = message.video || currentVideo;
    const session = getSession();
    const item = message.item;
    const previous = session.segments[session.segments.length - 1];
    const normalized = normalizeText(item.text).toLocaleLowerCase();
    if (previous && previous.sourceMethod === "visible_dom"
        && normalizeText(previous.text).toLocaleLowerCase() === normalized
        && item.start != null && previous.start != null
        && Math.abs(item.start - previous.start) <= 1.5) {
      previous.repeatObservations = (previous.repeatObservations || 1) + 1;
      previous.lastObservedAt = item.observedAt;
    } else {
      session.segments.push({
        id: item.id,
        text: item.text,
        start: item.start,
        end: null,
        duration: null,
        timestamp: item.timestamp,
        sourceMethod: "visible_dom",
        sourceType: "youtube_rendered_caption",
        observedAt: item.observedAt,
        repeatObservations: 1
      });
    }
    if (session.segments.length > 5000) session.segments = session.segments.slice(-5000);
    session.captionTrack.sourceMethod = "visible_dom";
    session.captionTrack.completeness = "partial";
    session.updatedAt = new Date().toISOString();
    await persist();
    setStatus("正在觀察 YouTube 可見字幕", true);
    render();
  }
});

$("refreshBtn").addEventListener("click", getActiveYouTubeTab);
$("clearBtn").addEventListener("click", async () => {
  if (!confirm("確定清除目前影片的字幕資料？此操作無法復原。")) return;
  delete sessions[sessionKey(currentVideo.videoId)];
  await persist();
  render();
  setStatus("目前影片資料已清除", true);
});

$("importTextBtn").addEventListener("click", async () => {
  try {
    const parsed = parseSubtitleFile($("transcriptInput").value, "pasted.txt");
    await importSegments(parsed.segments, "user_paste", parsed.format, $("languageInput").value.trim());
    $("transcriptInput").value = "";
  } catch (error) { alert(error.message || "匯入失敗"); }
});

$("fileInput").addEventListener("change", async (event) => {
  const file = event.target.files?.[0];
  if (!file) return;
  try {
    const parsed = parseSubtitleFile(await file.text(), file.name);
    await importSegments(parsed.segments, "user_file_import", parsed.format, $("languageInput").value.trim());
  } catch (error) { alert(error.message || "匯入失敗"); }
  event.target.value = "";
});

$("exportBtn").addEventListener("click", () => {
  const session = getSession();
  const output = {
    schemaVersion: "0.2",
    exportedAt: new Date().toISOString(),
    video: session.video,
    captionTrack: session.captionTrack,
    segments: session.segments
  };
  const blob = new Blob([JSON.stringify(output, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `youtube-captions-${currentVideo.videoId || "transcript"}.json`;
  a.click();
  URL.revokeObjectURL(url);
});

(async function init() {
  await loadSaved();
  await getActiveYouTubeTab();
  chrome.tabs.onActivated.addListener(getActiveYouTubeTab);
  chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
    if (tabId === activeTabId && changeInfo.status === "complete") getActiveYouTubeTab();
  });
})();