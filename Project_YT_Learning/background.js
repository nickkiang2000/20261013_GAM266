chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
});

chrome.runtime.onMessage.addListener((message, sender) => {
  if (message?.type === "YT_CAPTION_UPDATE" || message?.type === "YT_CAPTION_RESET") {
    // Side panel listens to runtime messages. No remote transmission or server storage.
  }
});