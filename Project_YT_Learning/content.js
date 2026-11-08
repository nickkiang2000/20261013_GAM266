(() => {
  if (window.__ytKnowledge02Installed) return;
  window.__ytKnowledge02Installed = true;

  let currentVideoId = getVideoId();
  let lastCanonicalText = "";
  let lastSeconds = -Infinity;
  let observedCount = 0;
  let observer = null;
  let pageObserver = null;

  function getVideoId() {
    try { return new URL(location.href).searchParams.get("v") || ""; }
    catch { return ""; }
  }

  function getVideo() {
    return document.querySelector("video.html5-main-video") || document.querySelector("video");
  }

  function getTitle() {
    return document.querySelector("h1.ytd-watch-metadata")?.textContent?.trim()
      || document.title.replace(/\s*-\s*YouTube\s*$/, "").trim()
      || "";
  }

  function formatTime(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return "00:00";
    const total = Math.floor(seconds);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    return h > 0
      ? `${String(h).padStart(2,"0")}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`
      : `${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`;
  }

  function resetIfVideoChanged() {
    const id = getVideoId();
    if (id !== currentVideoId) {
      currentVideoId = id;
      lastCanonicalText = "";
      lastSeconds = -Infinity;
      observedCount = 0;
      chrome.runtime.sendMessage({
        type: "YT_CAPTION_RESET",
        video: getVideoInfo()
      }).catch(() => {});
    }
  }

  function getVideoInfo() {
    return {
      videoId: currentVideoId,
      title: getTitle(),
      url: location.href
    };
  }

  function normalizeText(text) {
    return (text || "").replace(/\s+/g, " ").trim();
  }

  function readVisibleCaption() {
    resetIfVideoChanged();
    const nodes = Array.from(document.querySelectorAll(
      ".ytp-caption-window-container .ytp-caption-segment"
    ));
    const text = normalizeText(nodes
      .map(node => node.textContent || "")
      .filter(Boolean)
      .join(" "));

    if (!text) return;
    const canonical = text.toLocaleLowerCase();
    const video = getVideo();
    const seconds = video && Number.isFinite(video.currentTime) ? video.currentTime : 0;

    // Merge repeated DOM mutations of the same caption within a short playback-time
    // interval. Repeated words/phrases later in the video remain separate segments.
    if (canonical === lastCanonicalText && Math.abs(seconds - lastSeconds) <= 1.5) return;

    lastCanonicalText = canonical;
    lastSeconds = seconds;
    observedCount += 1;

    const item = {
      id: `${currentVideoId || "unknown"}-${Math.round(seconds * 1000)}-${observedCount}`,
      text,
      start: seconds,
      end: null,
      duration: null,
      timestamp: formatTime(seconds),
      videoId: currentVideoId,
      observedAt: new Date().toISOString(),
      sourceMethod: "visible_dom",
      sourceType: "youtube_rendered_caption"
    };

    chrome.runtime.sendMessage({
      type: "YT_CAPTION_UPDATE",
      item,
      video: getVideoInfo()
    }).catch(() => {});
  }

  function attachCaptionObserver() {
    if (!observer) observer = new MutationObserver(readVisibleCaption);
    const target = document.querySelector(".ytp-caption-window-container");
    if (!target) return;
    observer.disconnect();
    observer.observe(target, { childList: true, subtree: true, characterData: true });
  }

  pageObserver = new MutationObserver(() => {
    resetIfVideoChanged();
    if (document.querySelector(".ytp-caption-window-container")) attachCaptionObserver();
    readVisibleCaption();
  });
  pageObserver.observe(document.documentElement, { childList: true, subtree: true });

  document.addEventListener("yt-navigate-finish", () => {
    resetIfVideoChanged();
    setTimeout(attachCaptionObserver, 300);
  });

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message?.type === "YT_CAPTION_GET_STATE") {
      sendResponse({ video: getVideoInfo(), observedCount });
    }
    if (message?.type === "YT_CAPTION_SEEK") {
      const video = getVideo();
      if (video && Number.isFinite(message.seconds)) {
        video.currentTime = Math.max(0, message.seconds);
        sendResponse({ ok: true });
      } else {
        sendResponse({ ok: false });
      }
    }
    return true;
  });

  attachCaptionObserver();
  readVisibleCaption();
})();