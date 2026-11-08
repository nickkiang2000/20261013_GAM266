const PREFIX = "ytki03:";
let activeVideoId = "";
let activeData = null;

const $ = id => document.getElementById(id);
const clean = s => String(s || "").replace(/\s+/g, " ").trim();

function secondsFromTimestamp(s) {
  const m = String(s).trim().match(/^(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?:[.,](\d+))?$/);
  if (!m) return null;
  const h = Number(m[1] || 0), min = Number(m[2]), sec = Number(m[3]);
  if (min > 59 || sec > 59) return null;
  return h*3600 + min*60 + sec + (m[4] ? Number("0."+m[4]) : 0);
}

// Handles YouTube's copied UI text where the visible timestamp is concatenated
// with an accessible/localized label, e.g. "3:103 分 10 秒字幕".
function parseYouTubeCopiedLine(line) {
  const s = String(line || "").trim();
  const m = s.match(/^(\d{1,2}):(\d{2})(?:\s*(\d{1,2})\s*分\s*(\d{1,2})\s*秒|\s*(\d{1,2})\s*秒)?\s*(.*)$/u);
  if (!m) return null;
  const mm = Number(m[1]), ss = Number(m[2]);
  if (ss > 59) return null;
  // The visible m:ss is authoritative; the duplicated localized label is discarded.
  const text = clean(m[7] || m[6] || m[5] || "");
  if (!text) return null;
  return { start: mm*60+ss, end:null, duration:null, text };
}

function parseTranscript(raw) {
  const text = String(raw || "").replace(/\r/g, "").trim();
  if (!text) return [];
  const lines = text.split("\n").map(x => x.trim()).filter(Boolean);
  const out = [];
  let srtMode = false;
  for (let i=0; i<lines.length; i++) {
    const line = lines[i];
    const timeLine = line.match(/^(\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?)\s*-->\s*(\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?)/);
    if (timeLine) {
      srtMode = true;
      const start = secondsFromTimestamp(timeLine[1]), end = secondsFromTimestamp(timeLine[2]);
      const body = [];
      while (i+1 < lines.length && !/^\d+$/.test(lines[i+1]) && !/^\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?\s*-->/.test(lines[i+1])) body.push(lines[++i]);
      const value = clean(body.join(" "));
      if (start !== null && value) out.push({start,end,duration:end===null?null:end-start,text:value});
      continue;
    }
    const yt = parseYouTubeCopiedLine(line);
    if (yt) { out.push(yt); continue; }
    const prefix = line.match(/^\[?(\d{1,2}:\d{2}(?::\d{2})?)\]?\s+(.+)$/);
    if (prefix) {
      const start = secondsFromTimestamp(prefix[1]);
      if (start !== null) { out.push({start,end:null,duration:null,text:clean(prefix[2])}); continue; }
    }
    // Preserve plain text as one segment only when no timestamped cues were parsed.
  }
  if (out.length) {
    const unique = new Map();
    for (const seg of out) unique.set(`${seg.start}|${seg.text}`, seg);
    return [...unique.values()].sort((a,b)=>a.start-b.start).map((s,i)=>({id:`imp-${i+1}`,...s,sourceType:"user_pasted_transcript"}));
  }
  return [{id:"imp-1",start:null,end:null,duration:null,text:clean(lines.join(" ")),sourceType:"user_pasted_plain_text"}];
}

function fmtTime(sec) {
  if (!Number.isFinite(sec)) return "—";
  sec = Math.floor(sec);
  const h = Math.floor(sec/3600), m = Math.floor((sec%3600)/60), s = sec%60;
  return h ? `${h}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}` : `${m}:${String(s).padStart(2,"0")}`;
}

function renderSegments(target, segments, isObservation=false) {
  target.innerHTML = "";
  if (!segments?.length) {
    target.className = "segments empty";
    target.textContent = isObservation ? "尚無觀察紀錄" : "尚未取得逐字稿";
    return;
  }
  target.className = "segments";
  const frag = document.createDocumentFragment();
  const display = [...segments].sort((a,b) => {
    const aa = Number.isFinite(a.start) ? a.start : Number.isFinite(a.observedPlaybackTime) ? a.observedPlaybackTime : Infinity;
    const bb = Number.isFinite(b.start) ? b.start : Number.isFinite(b.observedPlaybackTime) ? b.observedPlaybackTime : Infinity;
    return aa-bb;
  });
  for (const seg of display) {
    const row = document.createElement("div"); row.className = "segment";
    const time = document.createElement("button"); time.className = "time";
    time.textContent = fmtTime(Number.isFinite(seg.start) ? seg.start : seg.observedPlaybackTime);
    if (Number.isFinite(seg.start)) time.addEventListener("click", () => chrome.tabs.query({active:true,currentWindow:true}, tabs => {
      if (tabs[0]?.id) chrome.tabs.sendMessage(tabs[0].id,{type:"YTKI_SEEK",seconds:seg.start});
    }));
    else time.disabled = true;
    const body = document.createElement("div"); body.className = "segmentText"; body.textContent = seg.text || "";
    row.append(time,body);
    if (isObservation && seg.repeatObservations > 1) {
      const count = document.createElement("span"); count.className = "repeat"; count.textContent = `×${seg.repeatObservations}`; row.append(count);
    }
    frag.append(row);
  }
  target.append(frag);
}

async function getActiveTabInfo() {
  const tabs = await chrome.tabs.query({active:true,currentWindow:true});
  const tab = tabs[0];
  if (!tab?.id || !tab.url?.includes("youtube.com/watch")) return null;
  return new Promise(resolve => {
    chrome.tabs.sendMessage(tab.id,{type:"YT_GET_VIDEO_INFO"},res=>resolve(chrome.runtime.lastError?null:res));
  });
}

async function loadData() {
  const info = await getActiveTabInfo();
  if (!info?.videoId) {
    $("videoTitle").textContent = "請先開啟 YouTube 影片";
    $("status").textContent = "未偵測到 YouTube 影片";
    return;
  }
  activeVideoId = info.videoId;
  $("videoTitle").textContent = info.title || info.videoId;
  const key = PREFIX + activeVideoId;
  const stored = await chrome.storage.local.get(key);
  activeData = stored[key] || {
    schemaVersion:4, video:{videoId:activeVideoId,title:info.title,url:info.url},
    transcript:{sourceType:"none",completeness:"not_captured",segments:[]}, liveObservations:[]
  };
  $("transcriptCount").textContent = activeData.transcript?.segments?.length || 0;
  $("analysisCount").textContent = activeData.transcript?.analysisSegments?.length || 0;
  $("observationCount").textContent = activeData.liveObservations?.length || 0;
  renderSegments($("transcriptList"),activeData.transcript?.segments || []);
  renderSegments($("observationList"),activeData.liveObservations || [],true);
  $("diagnostics").textContent = activeData.captureDiagnostics?.latest
    ? JSON.stringify(activeData.captureDiagnostics.latest, null, 2)
    : "尚無診斷資料";
  const status = activeData.transcript?.completeness || "等待自動擷取";
  if (activeData.lastCapture?.detail) {
    $("status").textContent = `${activeData.lastCapture.status}：${activeData.lastCapture.detail}`;
  } else {
    const count = activeData.transcript?.segments?.length || 0;
    const coverage = activeData.transcript?.coverage;
    const suffix = coverage?.duration
      ? `（最後 ${Math.floor(coverage.lastStart || 0)}s / 影片 ${Math.floor(coverage.duration)}s）`
      : "";
    const display = {
      caption_track_reaches_video_end: `已完整擷取 ${count} 段（字幕軌快速路徑）${suffix}`,
      captured_from_rendered_panel_reaches_video_end: `已完整擷取 ${count} 段（YouTube 逐字稿面板）${suffix}`,
      captured_from_rendered_panel_may_be_partial: `已擷取 ${count} 段，但可能不完整${suffix}`
    };
    $("status").textContent = display[status] || status;
  }
}

$("refreshBtn").addEventListener("click", async () => {
  const info = await getActiveTabInfo();
  if (!info?.videoId) return;
  $("status").textContent = "正在重新檢查…";
  chrome.tabs.query({active:true,currentWindow:true}, tabs => {
    if (tabs[0]?.id) chrome.tabs.sendMessage(tabs[0].id,{type:"YT_RECAPTURE",videoId:info.videoId},()=>setTimeout(loadData,1200));
  });
});

$("importBtn").addEventListener("click", async () => {
  const parsed = parseTranscript($("pasteArea").value);
  if (!activeVideoId || !parsed.length) { $("importStatus").textContent = "沒有可匯入的內容。"; return; }
  const key = PREFIX + activeVideoId;
  const stored = await chrome.storage.local.get(key);
  const data = stored[key] || activeData || {schemaVersion:4,video:{videoId:activeVideoId,title:$("videoTitle").textContent},liveObservations:[]};
  const utils = globalThis.YTKITranscriptUtils;
  const analysisSegments = utils?.buildAnalysisSegments(parsed) || [];
  const plainText = utils?.buildPlainText(parsed) || parsed.map(segment => segment.text).join("\n");
  data.schemaVersion = 4;
  data.transcript = {
    dataVersion:1,
    sourceType:"user_pasted_transcript",
    sourceMethod:"user_paste",
    language:null,
    completeness:"user_provided_not_verified",
    capturedAt:new Date().toISOString(),
    sourceResponse:{format:"user_import",interceptedFromPlayer:false},
    availableTracks:[],
    segmentRole:"raw_caption_cues",
    segments:parsed,
    analysisSegments,
    plainText,
    stats:utils?.transcriptStats(parsed,analysisSegments,plainText) || {rawSegmentCount:parsed.length,analysisSegmentCount:analysisSegments.length,characterCount:[...plainText].length},
    contentSha256:null
  };
  await chrome.storage.local.set({[key]:data});
  $("importStatus").textContent = `成功解析 ${parsed.length} 段；即時觀察紀錄未刪除。`;
  $("pasteArea").value = "";
  await loadData();
});

$("exportBtn").addEventListener("click", async () => {
  if (!activeVideoId) return;
  const key = PREFIX + activeVideoId;
  const stored = await chrome.storage.local.get(key);
  const data = stored[key] || activeData;
  if (!data) return;
  const blob = new Blob([JSON.stringify(data,null,2)],{type:"application/json"});
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a"); a.href=url; a.download=`youtube-captions-${activeVideoId}.json`; a.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
});

chrome.storage.onChanged.addListener((changes,area) => {
  if (area === "local" && activeVideoId && changes[PREFIX+activeVideoId]) loadData();
});
chrome.runtime.onMessage.addListener(msg => {
  if (msg?.type === "YT_AUTO_CAPTURE_STATUS" && msg.videoId === activeVideoId) {
    $("status").textContent = msg.status + (msg.detail ? `：${msg.detail}` : "");
    setTimeout(loadData,400);
  }
});
loadData();
