const statusText = document.getElementById("statusText");
const statusDot = document.getElementById("statusDot");
const videoIdEl = document.getElementById("videoId");
const countEl = document.getElementById("count");
const shownCountEl = document.getElementById("shownCount");
const captionList = document.getElementById("captionList");
const emptyState = document.getElementById("emptyState");

let activeTabId = null;
let items = [];
let currentVideoId = "";

function setStatus(text, ok = false) {
  statusText.textContent = text;
  statusDot.classList.toggle("ok", ok);
}

function render() {
  countEl.textContent = String(items.length);
  shownCountEl.textContent = `${items.length} 筆`;
  videoIdEl.textContent = currentVideoId || "—";
  captionList.replaceChildren();
  emptyState.hidden = items.length > 0;

  for (const item of items) {
    const li = document.createElement("li");
    const time = document.createElement("button");
    time.className = "time";
    time.type = "button";
    time.textContent = item.timestamp || "00:00";
    time.title = "在 YouTube 播放器跳至此時間（盡力而為）";
    time.addEventListener("click", async () => {
      if (!activeTabId) return;
      await chrome.tabs.sendMessage(activeTabId, {
        type: "YT_CAPTION_SEEK",
        seconds: item.seconds
      }).catch(() => {});
    });

    const text = document.createElement("span");
    text.className = "caption-text";
    text.textContent = item.text;
    li.append(time, text);
    captionList.append(li);
  }
}

async function getActiveYouTubeTab() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const tab = tabs[0];
  if (!tab?.id || !tab.url?.startsWith("https://www.youtube.com/")) {
    activeTabId = null;
    setStatus("請先切換到 YouTube 影片頁面", false);
    currentVideoId = "";
    items = [];
    render();
    return;
  }
  activeTabId = tab.id;
  try {
    const state = await chrome.tabs.sendMessage(tab.id, { type: "YT_CAPTION_GET_STATE" });
    currentVideoId = state.videoId || "";
    items = state.items || [];
    setStatus(items.length ? "正在觀察 YouTube 可見字幕" : "已連線，等待可見字幕", true);
    render();
  } catch (error) {
    setStatus("無法連線；請重新載入 YouTube 分頁", false);
  }
}

chrome.runtime.onMessage.addListener((message, sender) => {
  if (!message || sender.tab?.id !== activeTabId) return;
  if (message.type === "YT_CAPTION_RESET") {
    currentVideoId = message.videoId || "";
    items = [];
    setStatus("影片已切換，等待字幕…", true);
    render();
  }
  if (message.type === "YT_CAPTION_UPDATE") {
    currentVideoId = message.item.videoId || currentVideoId;
    // Avoid duplicate display entries if the same overlay mutation fires repeatedly.
    const previous = items[items.length - 1];
    if (!previous || previous.text !== message.item.text || previous.timestamp !== message.item.timestamp) {
      items.push(message.item);
      if (items.length > 100) items = items.slice(-100);
    }
    setStatus("正在觀察 YouTube 可見字幕", true);
    render();
  }
});

document.getElementById("refreshBtn").addEventListener("click", getActiveYouTubeTab);
document.getElementById("clearBtn").addEventListener("click", () => {
  items = [];
  render();
  setStatus("側邊欄顯示紀錄已清除；播放中的字幕仍會繼續收集", true);
});

chrome.tabs.onActivated.addListener(getActiveYouTubeTab);
chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (tabId === activeTabId && changeInfo.status === "complete") getActiveYouTubeTab();
});

getActiveYouTubeTab();