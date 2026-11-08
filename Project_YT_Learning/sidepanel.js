const PREFIX = "ytki03:";
let activeVideoId = "";
let activeData = null;
let lastTermVideoId = "";
const expandedTerms = new Set();

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

function wikipediaLanguage(language) {
  if (/^ja/i.test(language || "")) return "ja";
  if (/^zh/i.test(language || "")) return "zh";
  return "en";
}

function normalizedWikiTitle(value) {
  return clean(value).replace(/[＿_]/g, " ").toLocaleLowerCase();
}

function wikiMatchConfidence(term, title, index) {
  const a = normalizedWikiTitle(term), b = normalizedWikiTitle(title);
  if (a === b) return 1;
  if (b.startsWith(a) || a.startsWith(b)) return 0.9;
  if (b.includes(a) || a.includes(b)) return 0.76;
  return Math.max(0.42, 0.62 - index * 0.08);
}

async function searchWikipediaTerm(term, language) {
  const params = new URLSearchParams({
    action:"query", list:"search", srsearch:term, srnamespace:"0", srlimit:"3",
    srprop:"snippet", format:"json", formatversion:"2", origin:"*"
  });
  try {
    const response = await fetch(`https://${language}.wikipedia.org/w/api.php?${params}`, { credentials:"omit" });
    if (!response.ok) return null;
    const results = (await response.json())?.query?.search || [];
    if (!results.length) return null;
    const best=results.map((page,index)=>({
      pageId:page.pageid, title:page.title,
      matchConfidence:wikiMatchConfidence(term,page.title,index),
      searchSnippet:clean(String(page.snippet||"").replace(/<[^>]+>/g,""))
    })).sort((a,b)=>b.matchConfidence-a.matchConfidence)[0];
    return best?.matchConfidence>=0.7?best:null;
  } catch (_) { return null; }
}

async function fetchWikipediaExtracts(matches, language) {
  const byId = new Map();
  for (let i=0; i<matches.length; i+=20) {
    const batch = matches.slice(i,i+20).filter(Boolean);
    if (!batch.length) continue;
    const params = new URLSearchParams({
      action:"query", prop:"extracts|info", inprop:"url", exintro:"1", explaintext:"1",
      exchars:"360", pageids:batch.map(item=>item.pageId).join("|"),
      format:"json", formatversion:"2", origin:"*"
    });
    try {
      const response = await fetch(`https://${language}.wikipedia.org/w/api.php?${params}`, { credentials:"omit" });
      if (!response.ok) continue;
      for (const page of (await response.json())?.query?.pages || []) byId.set(page.pageid,page);
    } catch (_) {}
  }
  return byId;
}

async function enrichTermsWithWikipedia(terms, language) {
  const selected = terms.slice(0,25);
  const matches = new Array(selected.length);
  let cursor = 0;
  async function worker() {
    while (cursor < selected.length) {
      const index = cursor++;
      matches[index] = await searchWikipediaTerm(selected[index].term,language);
    }
  }
  await Promise.all(Array.from({length:Math.min(4,selected.length)},worker));
  const pages = await fetchWikipediaExtracts(matches,language);
  return terms.map((term,index)=>{
    const match = index < matches.length ? matches[index] : null;
    if (!match) return {...term,wikipedia:{status:index<25?"not_found":"not_queried",language},score:term.localScore};
    const page = pages.get(match.pageId);
    const explanation = clean(page?.extract || match.searchSnippet || "");
    return {
      ...term,
      score:Number(Math.min(100,term.localScore+match.matchConfidence*15).toFixed(2)),
      wikipedia:{
        status:"matched",language,title:match.title,pageId:match.pageId,
        url:page?.fullurl || `https://${language}.wikipedia.org/wiki/${encodeURIComponent(match.title.replace(/ /g,"_"))}`,
        matchConfidence:Number(match.matchConfidence.toFixed(2)),explanation
      }
    };
  }).sort((a,b)=>b.score-a.score||b.segmentCount-a.segmentCount||a.term.localeCompare(b.term));
}

async function resolveTermReference(term, primaryLanguage) {
  const languages = primaryLanguage === "en" ? ["en"] : [primaryLanguage, "en"];
  for (const language of languages) {
    const match = await searchWikipediaTerm(term.term, language);
    if (!match) continue;
    const pages = await fetchWikipediaExtracts([match], language);
    const page = pages.get(match.pageId);
    return {
      status: "matched", language, title: match.title, pageId: match.pageId,
      url: page?.fullurl || `https://${language}.wikipedia.org/wiki/${encodeURIComponent(match.title.replace(/ /g,"_"))}`,
      matchConfidence: Number(match.matchConfidence.toFixed(2)),
      explanation: clean(page?.extract || match.searchSnippet || "")
    };
  }
  return {
    status: "google_fallback",
    language: null,
    explanation: "原語言與英文 Wikipedia 均未找到可信詞條。",
    url: `https://www.google.com/search?q=${encodeURIComponent(term.term)}`
  };
}

async function ensureLocalKnowledgeTerms() {
  const transcript = activeData?.transcript;
  if (!activeVideoId || !transcript?.segments?.length) return;
  if (activeData.knowledgeTerms?.method === "rule_based_v2_lazy_lookup" &&
      activeData.knowledgeTerms.sourceTranscriptSha256 === (transcript.contentSha256 || null)) return;
  const utils = globalThis.YTKITranscriptUtils, extractor = globalThis.YTKITermExtractor;
  const analysisSegments = transcript.analysisSegments?.length
    ? transcript.analysisSegments : utils.buildAnalysisSegments(transcript.segments);
  const language = transcript.language || "en";
  const terms = extractor.extractTerms({
    analysisSegments, title: activeData.video?.title || "", language, limit: 200
  }).map(term => ({ ...term, wikipedia: { status: "not_queried", language: wikipediaLanguage(language) } }));
  const key = PREFIX + activeVideoId;
  const current = (await chrome.storage.local.get(key))[key] || activeData;
  current.knowledgeTerms = {
    version: 2, method: "rule_based_v2_lazy_lookup", generatedAt: new Date().toISOString(),
    sourceTranscriptSha256: current.transcript?.contentSha256 || null,
    language, wikipediaLanguage: wikipediaLanguage(language), candidateCount: terms.length, terms
  };
  await chrome.storage.local.set({ [key]: current });
}

function renderKnowledgeTerms() {
  const target=$("termList"), result=activeData?.knowledgeTerms;
  target.innerHTML="";
  if (!result?.terms?.length) {
    target.className="terms empty"; target.textContent="尚無專有名詞";
    $("termStatus").textContent=result?.error||"取得完整逐字稿後會自動分析";
    return;
  }
  target.className="terms";
  const frag=document.createDocumentFragment();
  result.terms.forEach((term,index)=>{
    const card=document.createElement("article"); card.className="termCard";
    const head=document.createElement("button"); head.className="termHead termToggle"; head.dataset.termIndex=String(index);
    const rank=document.createElement("span"); rank.className="termRank"; rank.textContent=`#${index+1}`;
    const name=document.createElement("span"); name.className="termName"; name.textContent=term.term;
    const score=document.createElement("span"); score.className="termScore"; score.textContent=`${Math.round(term.score)} 分`;
    head.append(rank,name,score);
    const meta=document.createElement("div"); meta.className="termMeta";
    meta.textContent=`${term.type}・出現 ${term.frequency} 次・跨 ${term.segmentCount} 段`;
    card.append(head,meta);
    if (expandedTerms.has(`${activeVideoId}:${index}`)) {
      const explanation=document.createElement("div"); explanation.className="termExplanation";
      const wiki=term.wikipedia || {status:"not_queried"};
      explanation.textContent=wiki.status==="not_queried" ? "正在查詢 Wikipedia…" : (wiki.explanation||"此詞條沒有可用的簡介。");
      const links=document.createElement("div"); links.className="termLinks";
      if(wiki.url){const a=document.createElement("a");a.className="wikiLink";a.href=wiki.url;a.target="_blank";a.rel="noopener";a.textContent=wiki.status==="google_fallback"?"Google 搜尋":`${wiki.language}.wikipedia`;links.append(a)}
      for(const occurrence of (term.occurrences||[])){
        const button=document.createElement("button");button.className="termTime";button.textContent=fmtTime(occurrence.start);button.dataset.seconds=occurrence.start;links.append(button)
      }
      card.append(explanation,links);
    }
    frag.append(card);
  });
  target.append(frag);
  const resolved=result.terms.filter(term=>term.wikipedia?.status && term.wikipedia.status!=="not_queried").length;
  $("termStatus").textContent=`已在本機找出 ${result.terms.length} 個候選；已展開查詢 ${resolved} 個`;
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
  if (lastTermVideoId !== activeVideoId) { expandedTerms.clear(); lastTermVideoId=activeVideoId; }
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
  renderKnowledgeTerms();
  ensureLocalKnowledgeTerms().catch(error => { $("termStatus").textContent=`本機分析失敗：${String(error?.message||error)}`; });
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

$("termList").addEventListener("click",async event=>{
  const timeButton=event.target.closest("button[data-seconds]");
  if(timeButton){
    const seconds=Number(timeButton.dataset.seconds);if(!Number.isFinite(seconds))return;
    chrome.tabs.query({active:true,currentWindow:true},tabs=>{if(tabs[0]?.id)chrome.tabs.sendMessage(tabs[0].id,{type:"YTKI_SEEK",seconds})});
    return;
  }
  const toggle=event.target.closest("button[data-term-index]");
  if(!toggle||!activeData?.knowledgeTerms?.terms)return;
  const index=Number(toggle.dataset.termIndex), keyId=`${activeVideoId}:${index}`;
  if(expandedTerms.has(keyId)){expandedTerms.delete(keyId);renderKnowledgeTerms();return}
  expandedTerms.add(keyId);renderKnowledgeTerms();
  const term=activeData.knowledgeTerms.terms[index];
  if(!term||term.wikipedia?.status!=="not_queried")return;
  try{
    const reference=await resolveTermReference(term,activeData.knowledgeTerms.wikipediaLanguage||"en");
    const storageKey=PREFIX+activeVideoId;
    const current=(await chrome.storage.local.get(storageKey))[storageKey]||activeData;
    const currentTerm=current.knowledgeTerms?.terms?.[index];
    if(currentTerm?.normalizedTerm===term.normalizedTerm){
      currentTerm.wikipedia=reference;
      await chrome.storage.local.set({[storageKey]:current});
    }
  }catch(error){
    term.wikipedia={status:"lookup_error",language:null,explanation:`查詢失敗：${String(error?.message||error)}`,
      url:`https://www.google.com/search?q=${encodeURIComponent(term.term)}`};
    renderKnowledgeTerms();
  }
});

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
