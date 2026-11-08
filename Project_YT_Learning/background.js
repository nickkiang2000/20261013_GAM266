chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "YT_AUTO_CAPTURE_STATUS") {
    chrome.runtime.sendMessage(message).catch(() => {});
    return;
  }
  if (message?.type === "YT_OPEN_SIDE_PANEL" && sender.tab?.id != null) {
    chrome.sidePanel.open({ tabId: sender.tab.id }).catch(() => {});
    sendResponse({ ok: true });
  }
  return true;
});
