(() => {
  if (window.__ytki03Installed) return;
  window.__ytki03Installed = true;

  const STORE_PREFIX = "ytki03:";
  let currentVideoId = "";
  let captionObserver = null;
  let lastCaptionText = "";
  let lastCaptionTime = 0;
  let panelOpenedByUs = false;
  let captureRunning = false;

  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const videoIdFromUrl = () => new URL(location.href).searchParams.get("v") || "";
  const videoEl = () => document.querySelector("video.html5-main-video") || document.querySelector("video");
  const getTitle = () => (document.querySelector("h1.ytd-watch-metadata")?.innerText || document.title || "").replace(/\s*-\s*YouTube$/, "").trim();

  function parseClock(value) {
    if (!value) return null;
    const s = value.trim().replace(/,/g, ".");
    const m = s.match(/^(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?:\.(\d+))?$/);
    if (!m) return null;
    const h = Number(m[1] || 0), min = Number(m[2]), sec = Number(m[3]);
    if (min > 59 || sec > 59) return null;
    return h * 3600 + min * 60 + sec + (m[4] ? Number("0."+m[4]) : 0);
  }

  function normalizeText(s) {
    return String(s || "").replace(/\s+/g, " ").trim().normalize("NFKC");
  }

  async function readStored(videoId) {
    const key = STORE_PREFIX + videoId;
    const data = await chrome.storage.local.get(null);
    if (data[key]) return data[key];

    // Best-effort migration from the 0.2 storage shape. Keep old keys untouched.
    const legacy = Object.values(data).find(value => value && value.video?.videoId === videoId && Array.isArray(value.segments));
    if (legacy) {
      const liveObservations = legacy.segments
        .filter(seg => seg.sourceMethod === "visible_dom" || seg.sourceType === "youtube_rendered_caption")
        .map((seg, i) => ({
          id: seg.id || `legacy-obs-${i+1}`,
          observedPlaybackTime: Number.isFinite(seg.start) ? seg.start : null,
          firstObservedAt: Number.isFinite(seg.start) ? seg.start : null,
          lastObservedAt: Number.isFinite(seg.start) ? seg.start : null,
          start: null, end: null, text: seg.text || "",
          repeatObservations: seg.repeatObservations || 1,
          sourceType: "youtube_rendered_caption_observation",
          timestampIsObservationTime: true
        })).filter(seg => seg.text);
      const pasted = legacy.segments.filter(seg => seg.sourceMethod === "user_paste" || seg.sourceType === "plain_text");
      const transcriptSegments = pasted.filter(seg => seg.text).map((seg, i) => ({
        id: `legacy-import-${i+1}`, start: Number.isFinite(seg.start) ? seg.start : null,
        end: Number.isFinite(seg.end) ? seg.end : null, duration: Number.isFinite(seg.duration) ? seg.duration : null,
        text: seg.text, sourceType: "legacy_user_import"
      }));
      return {
        schemaVersion: 3,
        video: legacy.video,
        transcript: {
          sourceType: transcriptSegments.length ? "legacy_user_import" : "none",
          sourceMethod: "migrated_from_0_2",
          language: legacy.captionTrack?.language || null,
          completeness: transcriptSegments.length ? "user_provided_not_verified" : "not_captured",
          segments: transcriptSegments
        },
        liveObservations,
        migratedFrom: "0.2",
        updatedAt: new Date().toISOString()
      };
    }
    return {
      schemaVersion: 3,
      video: { videoId, title: getTitle(), url: location.href.split("&")[0] },
      transcript: { sourceType: "none", language: null, completeness: "not_captured", segments: [] },
      liveObservations: [],
      updatedAt: new Date().toISOString()
    };
  }

  async function saveStored(data) {
    data.updatedAt = new Date().toISOString();
    await chrome.storage.local.set({ [STORE_PREFIX + data.video.videoId]: data });
    chrome.runtime.sendMessage({ type: "YT_AUTO_CAPTURE_STATUS", videoId: data.video.videoId,
      title: data.video.title, count: data.transcript?.segments?.length || 0,
      status: data.transcript?.completeness || "unknown" }).catch(() => {});
  }

  function setStatus(status, detail = "") {
    document.documentElement.dataset.ytki03Status = status;
    const id = videoIdFromUrl();
    if (id) {
      const key = STORE_PREFIX + id;
      chrome.storage.local.get(key).then(result => {
        const data = result[key] || {
          schemaVersion: 3,
          video: { videoId: id, title: getTitle(), url: location.href.split("&")[0] },
          transcript: { sourceType: "none", language: null, completeness: "not_captured", segments: [] },
          liveObservations: []
        };
        data.lastCapture = { status, detail, at: new Date().toISOString() };
        return chrome.storage.local.set({ [key]: data });
      }).catch(() => {});
    }
    chrome.runtime.sendMessage({ type: "YT_AUTO_CAPTURE_STATUS", videoId: id,
      title: getTitle(), status, detail }).catch(() => {});
  }

  function findTranscriptButton() {
    const preferred = [
      "ytd-video-description-transcript-section-renderer button",
      "ytd-video-description-transcript-section-renderer tp-yt-paper-button",
      "ytd-engagement-panel-section-list-renderer #header"
    ];
    for (const sel of preferred) {
      const el = document.querySelector(sel);
      if (el && /transcript|轉錄|逐字稿|文字記錄/i.test((el.innerText || "") + " " + (el.getAttribute("aria-label") || ""))) return el;
    }
    const candidates = [...document.querySelectorAll("button, tp-yt-paper-button, a, yt-button-shape button")];
    return candidates.find(el => {
      const text = normalizeText((el.innerText || "") + " " + (el.getAttribute("aria-label") || ""));
      return /show transcript|transcript|顯示轉錄稿|逐字稿|轉錄稿|文字記錄/i.test(text) &&
        el.getClientRects().length > 0;
    }) || null;
  }

  function transcriptPanel() {
    // YouTube has moved the transcript UI between engagement-panel containers
    // over time. Prefer known renderers, but fall back to the segment list.
    return document.querySelector("ytd-transcript-renderer") ||
      document.querySelector("ytd-transcript-segment-list-renderer") ||
      document.querySelector("ytd-engagement-panel-section-list-renderer[target-id*='transcript']") ||
      document.querySelector("ytd-engagement-panel-section-list-renderer[visibility='ENGAGEMENT_PANEL_VISIBILITY_EXPANDED']") ||
      null;
  }

  function transcriptRows() {
    const panel = transcriptPanel();
    const roots = panel ? [panel] : [];
    // Some YouTube layouts render transcript rows outside the expected renderer.
    roots.push(document);
    const found = new Set();
    for (const root of roots) {
      for (const row of root.querySelectorAll("ytd-transcript-segment-renderer, .segment, [class*='transcript-segment']")) {
        if (row !== document && row.getClientRects().length === 0) continue;
        found.add(row);
      }
    }
    return [...found];
  }

  function extractRows() {
    const result = [];
    for (const row of transcriptRows()) {
      const timeSelectors = [
        ".segment-timestamp", "#segment-timestamp", "[id='timestamp']",
        "[class*='timestamp']", "yt-formatted-string[aria-label*=':']"
      ];
      const textSelectors = [
        ".segment-text", "#segment-text", "[id='segment-text']",
        "[class*='segment-text']"
      ];
      let timeEl = null, textEl = null;
      for (const sel of timeSelectors) { timeEl = row.querySelector(sel); if (timeEl) break; }
      for (const sel of textSelectors) { textEl = row.querySelector(sel); if (textEl) break; }

      let timeText = normalizeText(
        timeEl?.innerText || timeEl?.textContent || timeEl?.getAttribute("aria-label") || ""
      );
      let text = normalizeText(textEl?.innerText || textEl?.textContent || "");

      // Fallback for markup variants: transcript row usually renders timestamp
      // and caption as separate text lines.
      if (!timeText || !text) {
        const lines = String(row.innerText || row.textContent || "")
          .split(/\n+/).map(normalizeText).filter(Boolean);
        if (!timeText && lines.length) timeText = lines.find(x => parseClock(x) !== null) || "";
        if (!text && lines.length) {
          const timeIndex = lines.findIndex(x => parseClock(x) !== null);
          text = lines.filter((x, i) => i !== timeIndex).join(" ");
        }
      }

      const start = parseClock(timeText);
      if (start !== null && text) result.push({ start, end: null, duration: null, text });
    }
    const unique = new Map();
    for (const seg of result) unique.set(`${seg.start}|${seg.text}`, seg);
    return [...unique.values()];
  }

  async function scrollTranscriptToLoadAll() {
    const panel = transcriptPanel();
    const scroller = (panel && (
      panel.querySelector("#segments-container, .segments-container, ytd-transcript-segment-list-renderer") ||
      panel
    )) || document.querySelector("#segments-container, .segments-container, ytd-transcript-segment-list-renderer");
    const collected = new Map();
    let stableRounds = 0;
    let previousCount = -1;
    for (let i = 0; i < 100; i++) {
      for (const seg of extractRows()) collected.set(`${seg.start}|${seg.text}`, seg);
      const count = collected.size;
      stableRounds = count === previousCount ? stableRounds + 1 : 0;
      if (stableRounds >= 4) break;
      previousCount = count;
      if (!scroller) break;
      const before = scroller.scrollTop;
      scroller.scrollTop = Math.min(scroller.scrollTop + Math.max(300, scroller.clientHeight * 0.8), scroller.scrollHeight);
      scroller.dispatchEvent(new Event("scroll", { bubbles: true }));
      await sleep(250);
      if (scroller.scrollTop === before && i > 5) break;
    }
    for (const seg of extractRows()) collected.set(`${seg.start}|${seg.text}`, seg);
    return [...collected.values()].sort((a,b) => a.start - b.start);
  }

  async function waitForPanel(timeout = 12000) {
    const start = Date.now();
    while (Date.now() - start < timeout) {
      if (extractRows().length) return true;
      await sleep(300);
    }
    return false;
  }

  async function autoCaptureTranscript(videoId) {
    if (captureRunning || videoId !== videoIdFromUrl()) return;
    captureRunning = true;
    setStatus("checking_transcript");
    try {
      let panel = transcriptPanel();
      if (!panel) {
        const btn = findTranscriptButton();
        if (!btn) {
          setStatus("transcript_unavailable", "找不到 YouTube 原生逐字稿入口；可能沒有字幕、面板尚未載入或 YouTube 介面已變更。");
          return;
        }
        panelOpenedByUs = true;
        btn.click();
        await sleep(500);
      }
      const ready = await waitForPanel();
      if (!ready) {
        setStatus("transcript_not_loaded", "已嘗試開啟逐字稿面板，但沒有讀到字幕行。");
        return;
      }
      const segments = await scrollTranscriptToLoadAll();
      if (!segments.length) {
        setStatus("transcript_empty", "面板已開啟，但未讀到可解析的字幕行。");
        return;
      }
      const data = await readStored(videoId);
      data.video = { videoId, title: getTitle(), url: location.href.split("&")[0] };
      data.transcript = {
        sourceType: "youtube_transcript_panel_dom",
        sourceMethod: "auto_open_and_read_dom",
        language: null,
        completeness: "captured_from_rendered_panel_not_independently_verified",
        capturedAt: new Date().toISOString(),
        segments: segments.map((s, i) => ({ id: `tr-${i+1}`, ...s, sourceType: "youtube_transcript_panel_dom" }))
      };
      await saveStored(data);
      setStatus("transcript_captured", `${segments.length} 段`);
      if (panelOpenedByUs) {
        await sleep(300);
        const close = document.querySelector("ytd-transcript-renderer #close-button, ytd-transcript-renderer button[aria-label*='Close'], ytd-transcript-renderer button[aria-label*='關閉']");
        if (close) close.click();
        panelOpenedByUs = false;
      }
    } catch (err) {
      setStatus("capture_error", String(err?.message || err));
    } finally {
      captureRunning = false;
    }
  }

  function startCaptionObserver() {
    if (captionObserver) captionObserver.disconnect();
    const root = document.querySelector(".ytp-caption-window-container");
    if (!root) {
      setTimeout(() => { if (videoIdFromUrl() === currentVideoId) startCaptionObserver(); }, 1200);
      return;
    }
    captionObserver = new MutationObserver(async () => {
      const v = videoEl();
      const id = videoIdFromUrl();
      if (!v || !id) return;
      const text = normalizeText([...root.querySelectorAll(".ytp-caption-segment")].map(x => x.innerText).join(" "));
      if (!text) return;
      const now = v.currentTime || 0;
      const data = await readStored(id);
      const arr = data.liveObservations || [];
      const prev = arr[arr.length - 1];
      const near = prev && now >= (prev.lastObservedAt ?? prev.observedPlaybackTime ?? 0) &&
        now - (prev.lastObservedAt ?? prev.observedPlaybackTime ?? 0) <= 5.5;
      if (near && text === prev.text) {
        prev.repeatObservations = (prev.repeatObservations || 1) + 1;
        prev.lastObservedAt = now;
      } else if (near && text.startsWith(prev.text) && text.length > prev.text.length) {
        prev.text = text;
        prev.lastObservedAt = now;
        prev.repeatObservations = (prev.repeatObservations || 1) + 1;
      } else if (!(near && prev.text.startsWith(text))) {
        arr.push({
          id: `obs-${Date.now()}-${arr.length}`,
          observedPlaybackTime: now, firstObservedAt: now, lastObservedAt: now,
          start: null, end: null, text, repeatObservations: 1,
          sourceType: "youtube_rendered_caption_observation",
          timestampIsObservationTime: true
        });
      }
      data.liveObservations = arr.slice(-5000);
      await saveStored(data);
      lastCaptionText = text;
      lastCaptionTime = now;
    });
    captionObserver.observe(root, { childList: true, subtree: true, characterData: true });
  }

  async function onVideoChanged() {
    const id = videoIdFromUrl();
    if (!id || id === currentVideoId) return;
    currentVideoId = id;
    lastCaptionText = "";
    lastCaptionTime = 0;
    panelOpenedByUs = false;
    setStatus("video_detected");
    const data = await readStored(id);
    data.video = { videoId: id, title: getTitle(), url: location.href.split("&")[0] };
    await saveStored(data);
    setTimeout(() => autoCaptureTranscript(id), 1400);
    setTimeout(startCaptionObserver, 900);
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message?.type === "YTKI_RECAPTURE" && message.videoId === videoIdFromUrl()) {
      autoCaptureTranscript(message.videoId).then(() => sendResponse({ ok: true })).catch(e => sendResponse({ ok:false, error:String(e) }));
      return true;
    }
    if (message?.type === "YTKI_SEEK" && Number.isFinite(message.seconds)) {
      const v = videoEl();
      if (v) { v.currentTime = message.seconds; v.play().catch(() => {}); sendResponse({ ok: true }); }
      return true;
    }
    if (message?.type === "YT_GET_VIDEO_INFO") {
      sendResponse({ videoId: videoIdFromUrl(), title: getTitle(), url: location.href });
      return true;
    }
  });

  document.addEventListener("yt-navigate-finish", () => setTimeout(onVideoChanged, 300));
  window.addEventListener("popstate", () => setTimeout(onVideoChanged, 300));
  const urlPoll = setInterval(() => { if (videoIdFromUrl() !== currentVideoId) onVideoChanged(); }, 1000);
  onVideoChanged();
})();
