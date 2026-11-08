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
  let latestCaptionTracks = [];
  let captionTrackWaiters = [];

  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const videoIdFromUrl = () => new URL(location.href).searchParams.get("v") || "";
  const videoEl = () => document.querySelector("video.html5-main-video") || document.querySelector("video");
  const getTitle = () => (document.querySelector("h1.ytd-watch-metadata")?.innerText || document.title || "").replace(/\s*-\s*YouTube$/, "").trim();

  // MAIN-world page scripts can see YouTube's player state while an ordinary
  // content script cannot. Treat its messages as untrusted page input and
  // validate every URL again before fetching it.
  window.addEventListener("message", event => {
    if (event.source !== window || event.origin !== location.origin || event.data?.type !== "ytki03:caption-tracks") return;
    const tracks = Array.isArray(event.data.tracks) ? event.data.tracks : [];
    latestCaptionTracks = tracks.filter(track => track && typeof track.baseUrl === "string").slice(0, 80);
    const waiters = captionTrackWaiters;
    captionTrackWaiters = [];
    waiters.forEach(resolve => resolve(latestCaptionTracks));
  });

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

  function isSafeTimedTextUrl(value) {
    try {
      const url = new URL(value);
      return url.protocol === "https:" &&
        (url.hostname === "youtube.com" || url.hostname.endsWith(".youtube.com")) &&
        /^\/api\/timedtext$/.test(url.pathname);
    } catch (_) {
      return false;
    }
  }

  function requestCaptionTracks(timeout = 1600) {
    return new Promise(resolve => {
      const timer = setTimeout(() => {
        captionTrackWaiters = captionTrackWaiters.filter(waiter => waiter !== done);
        resolve(latestCaptionTracks);
      }, timeout);
      const done = tracks => { clearTimeout(timer); resolve(tracks); };
      captionTrackWaiters.push(done);
      window.postMessage({ type: "ytki03:request-caption-tracks" }, location.origin);
    });
  }

  function chooseCaptionTrack(tracks) {
    const preferences = [
      ...(navigator.languages || []).map(x => x.toLowerCase()),
      "zh-hant", "zh-tw", "zh", "en"
    ];
    const score = track => {
      const lang = String(track.languageCode || "").toLowerCase();
      const languageRank = preferences.findIndex(pref => lang === pref || lang.startsWith(pref + "-"));
      // Prefer a human-authored track when language suitability is identical.
      return (languageRank < 0 ? 1000 : languageRank * 10) + (track.kind === "asr" ? 1 : 0);
    };
    return tracks.filter(track => isSafeTimedTextUrl(track.baseUrl)).sort((a, b) => score(a) - score(b))[0] || null;
  }

  function segmentsFromJson3(data) {
    const result = [];
    for (const event of data?.events || []) {
      const text = normalizeText((event.segs || []).map(seg => seg.utf8 || "").join(""));
      const start = Number(event.tStartMs) / 1000;
      const duration = Number(event.dDurationMs) / 1000;
      if (text && Number.isFinite(start)) result.push({ start, end: Number.isFinite(duration) ? start + duration : null, duration, text });
    }
    const unique = new Map();
    for (const segment of result) unique.set(`${segment.start}|${segment.text}`, segment);
    return [...unique.values()].sort((a, b) => a.start - b.start);
  }

  async function tryDirectCaptionTrack() {
    const tracks = await requestCaptionTracks();
    const track = chooseCaptionTrack(tracks);
    if (!track) return null;
    const url = new URL(track.baseUrl);
    url.searchParams.set("fmt", "json3");
    try {
      const response = await fetch(url.href, { credentials: "include" });
      if (!response.ok) return null;
      const body = await response.text();
      // A current YouTube anti-abuse variant returns HTTP 200 with an empty
      // body. It is a failed fast path, not an empty transcript.
      if (!body.trim()) return null;
      const segments = segmentsFromJson3(JSON.parse(body));
      if (!segments.length) return null;
      const coverage = transcriptCoverage(segments, Number(videoEl()?.duration) || null);
      return { segments, coverage, language: track.languageCode || null, trackType: track.kind === "asr" ? "ASR" : "manual" };
    } catch (_) {
      return null;
    }
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

  function isRendered(el) {
    return !!el && el.getClientRects().length > 0;
  }

  function findTranscriptButton() {
    const preferred = [
      "ytd-video-description-transcript-section-renderer button",
      "ytd-video-description-transcript-section-renderer tp-yt-paper-button",
      "ytd-engagement-panel-section-list-renderer #header"
    ];
    for (const sel of preferred) {
      for (const el of document.querySelectorAll(sel)) {
        if (isRendered(el) && /show transcript|transcript|顯示轉錄稿|逐字稿|轉錄稿|文字記錄/i.test(
          (el.innerText || "") + " " + (el.getAttribute("aria-label") || "")
        )) return el;
      }
    }
    const candidates = [...document.querySelectorAll("button, tp-yt-paper-button, a, yt-button-shape button")];
    return candidates.find(el => {
      const text = normalizeText((el.innerText || "") + " " + (el.getAttribute("aria-label") || ""));
      return /show transcript|transcript|顯示轉錄稿|逐字稿|轉錄稿|文字記錄/i.test(text) &&
        isRendered(el);
    }) || null;
  }

  async function revealTranscriptButton() {
    let button = findTranscriptButton();
    if (button) return button;
    // In some layouts YouTube keeps the transcript CTA inside the collapsed
    // description. Expanding the description is preferable to clicking a
    // hidden duplicate button, which has no effect.
    const selectors = [
      "#description-inline-expander #expand",
      "ytd-text-inline-expander #expand",
      "tp-yt-paper-button#expand",
      "button[aria-label*='more' i]",
      "button[aria-label*='更多']"
    ];
    for (const selector of selectors) {
      const expander = [...document.querySelectorAll(selector)].find(isRendered);
      if (!expander) continue;
      expander.click();
      await sleep(350);
      button = findTranscriptButton();
      if (button) return button;
    }
    return null;
  }

  function transcriptPanel() {
    // YouTube has moved the transcript UI between engagement-panel containers
    // over time. Prefer known renderers, but fall back to the segment list.
    const candidates = [
      ...document.querySelectorAll("ytd-transcript-renderer"),
      ...document.querySelectorAll("ytd-transcript-segment-list-renderer"),
      ...document.querySelectorAll("ytd-engagement-panel-section-list-renderer[target-id*='transcript' i]"),
      ...document.querySelectorAll("ytd-engagement-panel-section-list-renderer[visibility='ENGAGEMENT_PANEL_VISIBILITY_EXPANDED']")
    ];
    return candidates.find(isRendered) || null;
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

  function findTranscriptScroller(panel) {
    // The segment list itself is not consistently the scroll container. YouTube
    // has used the list, the transcript renderer, and an ancestor engagement
    // panel across layouts. Pick a genuinely scrollable element, not just the
    // first matching renderer.
    const candidates = [];
    const add = el => {
      if (el && !candidates.includes(el)) candidates.push(el);
    };
    add(panel?.querySelector("#segments-container, .segments-container"));
    add(panel?.querySelector("ytd-transcript-segment-list-renderer"));
    add(panel);
    let ancestor = panel?.parentElement;
    for (let i = 0; ancestor && i < 7; i++, ancestor = ancestor.parentElement) add(ancestor);
    for (const el of candidates) {
      if (el.scrollHeight > el.clientHeight + 2) return el;
    }
    return candidates[0] || null;
  }

  function transcriptCoverage(segments, duration) {
    const lastStart = segments.reduce((max, seg) => Math.max(max, seg.start || 0), 0);
    // Captions commonly finish a few seconds before media.duration. Do not call
    // a transcript complete merely because its row count stopped changing.
    const tolerance = duration ? Math.max(12, Math.min(45, duration * 0.025)) : null;
    return {
      lastStart,
      duration,
      reachesEnd: duration ? lastStart >= duration - tolerance : false
    };
  }

  async function scrollTranscriptToLoadAll() {
    const panel = transcriptPanel();
    const scroller = findTranscriptScroller(panel);
    const collected = new Map();
    const duration = Number(videoEl()?.duration) || null;
    // Long transcripts are virtualized. Four 250ms no-growth checks (the old
    // logic) can end before the next chunk appears. Use a duration-scaled
    // deadline and page through the list so every virtualized row crosses the
    // viewport at least once.
    const deadline = Date.now() + Math.min(240000, Math.max(30000, (duration || 1800) * 1000 / 60));
    let idleRounds = 0;
    let lastCount = 0;
    let bottomRounds = 0;

    if (scroller) {
      scroller.scrollTop = 0;
      scroller.dispatchEvent(new Event("scroll", { bubbles: true }));
      await sleep(350);
    }

    while (Date.now() < deadline) {
      for (const seg of extractRows()) collected.set(`${seg.start}|${seg.text}`, seg);
      const segments = [...collected.values()];
      const coverage = transcriptCoverage(segments, duration);
      const atBottom = !scroller || scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 3;
      if (coverage.reachesEnd && atBottom) break;

      if (segments.length === lastCount) idleRounds++; else idleRounds = 0;
      lastCount = segments.length;
      if (!scroller) break;

      const before = scroller.scrollTop;
      const step = Math.max(120, Math.floor(scroller.clientHeight * 0.72));
      scroller.scrollTop = Math.min(before + step, scroller.scrollHeight);
      scroller.dispatchEvent(new Event("scroll", { bubbles: true }));
      await sleep(atBottom ? 500 : 300);

      const stillAtBottom = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 3;
      bottomRounds = stillAtBottom && scroller.scrollTop === before ? bottomRounds + 1 : 0;
      // Only give up after sustained no-progress at the actual bottom. The
      // result remains explicitly marked partial below unless it reaches end.
      if (bottomRounds >= 12 || (idleRounds >= 28 && stillAtBottom)) break;
    }
    for (const seg of extractRows()) collected.set(`${seg.start}|${seg.text}`, seg);
    const segments = [...collected.values()].sort((a,b) => a.start - b.start);
    return { segments, coverage: transcriptCoverage(segments, duration) };
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
      const direct = await tryDirectCaptionTrack();
      if (direct?.coverage?.reachesEnd) {
        const data = await readStored(videoId);
        data.video = { videoId, title: getTitle(), url: location.href.split("&")[0] };
        data.transcript = {
          sourceType: "youtube_caption_track",
          sourceMethod: "player_caption_track_json3",
          language: direct.language,
          trackType: direct.trackType,
          completeness: "caption_track_reaches_video_end",
          capturedAt: new Date().toISOString(),
          coverage: direct.coverage,
          segments: direct.segments.map((s, i) => ({ id: `tr-${i+1}`, ...s, sourceType: "youtube_caption_track" }))
        };
        await saveStored(data);
        setStatus("transcript_captured_complete", `${direct.segments.length} 段（字幕軌快速取得）`);
        return;
      }

      let panel = transcriptPanel();
      if (!panel) {
        const btn = await revealTranscriptButton();
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
      const capture = await scrollTranscriptToLoadAll();
      const { segments, coverage } = capture;
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
        completeness: coverage.reachesEnd
          ? "captured_from_rendered_panel_reaches_video_end"
          : "captured_from_rendered_panel_may_be_partial",
        capturedAt: new Date().toISOString(),
        coverage,
        segments: segments.map((s, i) => ({ id: `tr-${i+1}`, ...s, sourceType: "youtube_transcript_panel_dom" }))
      };
      await saveStored(data);
      const endText = coverage.duration
        ? `；最後時間 ${Math.floor(coverage.lastStart)}s / 影片 ${Math.floor(coverage.duration)}s`
        : "；無法讀取影片總長度，尚未驗證完整性";
      setStatus(coverage.reachesEnd ? "transcript_captured_complete" : "transcript_captured_partial", `${segments.length} 段${endText}`);
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
