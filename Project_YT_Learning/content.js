(() => {
  // Avoid installing twice if YouTube performs an in-page navigation.
  if (window.__ytCaptionPrototypeInstalled) return;
  window.__ytCaptionPrototypeInstalled = true;

  let lastText = "";
  let lastVideoId = new URL(location.href).searchParams.get("v") || "";
  let captionsObserved = [];

  function getVideoId() {
    return new URL(location.href).searchParams.get("v") || "";
  }

  function getVideoElement() {
    return document.querySelector("video.html5-main-video") || document.querySelector("video");
  }

  function formatTime(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return "00:00";
    const total = Math.floor(seconds);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    return hours > 0
      ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`
      : `${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  }

  function resetForVideoChange() {
    const currentId = getVideoId();
    if (currentId !== lastVideoId) {
      lastVideoId = currentId;
      lastText = "";
      captionsObserved = [];
      chrome.runtime.sendMessage({
        type: "YT_CAPTION_RESET",
        videoId: currentId
      }).catch(() => {});
    }
  }

  function readVisibleCaption() {
    resetForVideoChange();

    // YouTube's on-screen caption overlay. This is deliberately not a transcript
    // endpoint: it only exposes text currently rendered in the player.
    const segments = Array.from(document.querySelectorAll(".ytp-caption-window-container .ytp-caption-segment"));
    const text = segments
      .map(node => (node.textContent || "").replace(/\s+/g, " ").trim())
      .filter(Boolean)
      .join(" ")
      .trim();

    if (!text || text === lastText) return;
    lastText = text;

    const video = getVideoElement();
    const seconds = video && Number.isFinite(video.currentTime) ? video.currentTime : 0;
    const item = {
      text,
      seconds,
      timestamp: formatTime(seconds),
      videoId: lastVideoId,
      observedAt: new Date().toISOString()
    };

    captionsObserved.push(item);
    // Keep this prototype bounded; it is only a short-session observation log.
    if (captionsObserved.length > 500) captionsObserved = captionsObserved.slice(-500);

    chrome.runtime.sendMessage({
      type: "YT_CAPTION_UPDATE",
      item,
      count: captionsObserved.length
    }).catch(() => {});
  }

  const observer = new MutationObserver(() => readVisibleCaption());
  function attachObserver() {
    const target = document.querySelector(".ytp-caption-window-container") || document.body;
    observer.disconnect();
    observer.observe(target, { childList: true, subtree: true, characterData: true });
  }

  // Captions may be enabled after the page loads, so observe the document and
  // reattach to the caption container when it appears.
  const pageObserver = new MutationObserver(() => {
    if (document.querySelector(".ytp-caption-window-container")) attachObserver();
    readVisibleCaption();
  });
  pageObserver.observe(document.documentElement, { childList: true, subtree: true });

  document.addEventListener("yt-navigate-finish", () => {
    resetForVideoChange();
    attachObserver();
  });

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message?.type === "YT_CAPTION_GET_STATE") {
      sendResponse({
        videoId: getVideoId(),
        lastItem: captionsObserved[captionsObserved.length - 1] || null,
        items: captionsObserved.slice(-100),
        count: captionsObserved.length
      });
    }
  });

  attachObserver();
  readVisibleCaption();
})();