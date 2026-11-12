const PREFIX = "ytki03:";
let activeVideoId = "";
let activeData = null;
let lastTermVideoId = "";
const expandedTerms = new Set();
let showAllTerms = false;
let playingTermIndexes = new Set();
let preloadKey = "";
let loadGeneration = 0;
let tabReloadTimer = null;
let activeTabId = null;
let observedTabKey = "";
const priorityLookups = new Map();

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

function languageLabel(language) {
  const value=String(language||"").toLowerCase();
  if(value.startsWith("zh-hant")||value==="zh-tw")return "中文（繁體）";
  if(value.startsWith("zh-hans")||value==="zh-cn")return "中文（簡體）";
  if(value.startsWith("zh"))return "中文";
  if(value.startsWith("en"))return "英文";
  if(value.startsWith("ja"))return "日文";
  return language||"未知語言";
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
    const candidates=results.map((page,index)=>({
      pageId:page.pageid, title:page.title,
      matchConfidence:wikiMatchConfidence(term,page.title,index),
      searchSnippet:clean(String(page.snippet||"").replace(/<[^>]+>/g,""))
    })).sort((a,b)=>b.matchConfidence-a.matchConfidence);
    const pages=await fetchWikipediaExtracts(candidates,language);
    const best=candidates.find(candidate=>{
      const page=pages.get(candidate.pageId),extract=clean(page?.extract||candidate.searchSnippet||"");
      return page&&!page.pageprops?.disambiguation&&!/(?:可以指|可能指的是|may refer to|can refer to)/i.test(extract);
    });
    return best?.matchConfidence>=0.7?best:null;
  } catch (_) { return null; }
}

async function exactWikipediaTerm(term, language) {
  const params = new URLSearchParams({
    action:"query", prop:"extracts|info|pageprops", inprop:"url", exintro:"1", explaintext:"1", exchars:"360",
    titles:term, redirects:"1", converttitles:"1", format:"json", formatversion:"2", origin:"*"
  });
  try {
    const response=await fetch(`https://${language}.wikipedia.org/w/api.php?${params}`,{credentials:"omit"});
    if(!response.ok)return null;
    const page=(await response.json())?.query?.pages?.[0];
    const explanation=clean(page?.extract||"");
    if(!page||page.missing||page.pageprops?.disambiguation||/(?:可以指|可能指的是|may refer to|can refer to)/i.test(explanation))return null;
    return {status:"matched",language,title:page.title,pageId:page.pageid,url:page.fullurl,
      matchConfidence:1,explanation};
  }catch(_){return null}
}

async function fetchWikipediaExtracts(matches, language) {
  const byId = new Map();
  for (let i=0; i<matches.length; i+=20) {
    const batch = matches.slice(i,i+20).filter(Boolean);
    if (!batch.length) continue;
    const params = new URLSearchParams({
      action:"query", prop:"extracts|info|pageprops", inprop:"url", exintro:"1", explaintext:"1",
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
    const lookupTerms=[...new Set([term.term,...(term.lookupTerms||[])])];
    for(const lookupTerm of lookupTerms){const exact=await exactWikipediaTerm(lookupTerm,language);if(exact)return exact}
    let match=null;
    for(const lookupTerm of lookupTerms){match=await searchWikipediaTerm(lookupTerm,language);if(match)break}
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

function refineOccurrencesFromRawCues(terms, rawSegments) {
  const compact=value=>clean(value).toLocaleLowerCase().replace(/[^\p{L}\p{N}]/gu,"");
  const cues=[...(rawSegments||[])].filter(cue=>Number.isFinite(cue.start)&&cue.text).sort((a,b)=>a.start-b.start);
  return terms.map(term=>{
    const needles=[...new Set([term.term,...(term.lookupTerms||[])].map(compact).filter(value=>value.length>=2))];
    const occurrences=[];
    for(let i=0;i<cues.length;i++){
      const firstCue=compact(cues[i].text);
      const relevantNeedles=needles.filter(needle=>firstCue.includes(needle)||firstCue.includes(needle.slice(0,Math.min(3,needle.length))));
      if(!relevantNeedles.length)continue;
      let joined="",matchedEnd=-1;
      for(let j=i;j<Math.min(cues.length,i+8);j++){
        joined+=compact(cues[j].text);
        if(relevantNeedles.some(needle=>joined.includes(needle))){matchedEnd=j;break}
      }
      if(matchedEnd<0)continue;
      const last=cues[matchedEnd],next=cues[matchedEnd+1];
      occurrences.push({
        start:cues[i].start,
        end:Number.isFinite(last.end)&&last.end>cues[i].start?last.end:(Number.isFinite(next?.start)?next.start:last.start+6),
        text:cues.slice(i,matchedEnd+1).map(cue=>clean(cue.text)).join(" ").slice(0,260),
        sourceSegmentIds:cues.slice(i,matchedEnd+1).map(cue=>cue.id).filter(Boolean)
      });
      i=matchedEnd;
    }
    return occurrences.length?{...term,frequency:occurrences.length,segmentCount:occurrences.length,occurrences}:term;
  }).sort((a,b)=>(a.occurrences[0]?.start??Infinity)-(b.occurrences[0]?.start??Infinity)||b.score-a.score);
}

async function ensureLocalKnowledgeTerms() {
  const transcript = activeData?.transcript;
  if (!activeVideoId || !transcript?.segments?.length) return;
  if (activeData.knowledgeTerms?.method === "rule_based_v8_categorized_entities" &&
      activeData.knowledgeTerms.sourceTranscriptSha256 === (transcript.contentSha256 || null)) return;
  const utils = globalThis.YTKITranscriptUtils, extractor = globalThis.YTKITermExtractor;
  // Grouped text finds entities split across adjacent caption cues. After
  // extraction, map each entity back to the smallest matching raw-cue window
  // so playback highlighting stays precise.
  const analysisSegments=transcript.analysisSegments?.length
    ? transcript.analysisSegments:utils.buildAnalysisSegments(transcript.segments);
  const language = transcript.language || "en";
  const terms = refineOccurrencesFromRawCues(extractor.extractTerms({
    analysisSegments, title: activeData.video?.title || "", language, limit: 120
  }),transcript.segments).map(term => ({ ...term, wikipedia: { status: "not_queried", language: wikipediaLanguage(language) } }));
  const key = PREFIX + activeVideoId;
  const current = (await chrome.storage.local.get(key))[key] || activeData;
  current.knowledgeTerms = {
    version: 8, method: "rule_based_v8_categorized_entities", generatedAt: new Date().toISOString(),
    sourceTranscriptSha256: current.transcript?.contentSha256 || null,
    language, nativeLanguage:"zh", wikipediaLanguage: wikipediaLanguage(language), candidateCount: terms.length,
    defaultDisplayCount:60, terms
  };
  await chrome.storage.local.set({ [key]: current });
}

async function preloadKnowledgeDetails() {
  const result=activeData?.knowledgeTerms;
  if(!activeVideoId||!result?.terms?.length)return;
  const keyId=`${activeVideoId}:${result.sourceTranscriptSha256||"none"}:v8`;
  if(preloadKey===keyId)return;
  const pending=result.terms.map((term,index)=>({term,index})).filter(item=>item.term.wikipedia?.status==="not_queried");
  if(!pending.length){preloadKey=keyId;return}
  preloadKey=keyId;
  $("termStatus").textContent=`正在背景載入 ${pending.length} 個詞條的簡介與連結…`;
  let cursor=0,completed=0;
  async function worker(){
    while(cursor<pending.length){
      const item=pending[cursor++];
      item.term.wikipedia=await resolveTermReference(item.term,result.wikipediaLanguage||"en");
      completed++;
      if(completed%5===0)$("termStatus").textContent=`正在背景載入詞條 ${completed}/${pending.length}…`;
    }
  }
  await Promise.all(Array.from({length:Math.min(4,pending.length)},worker));
  const storageKey=PREFIX+activeVideoId,current=(await chrome.storage.local.get(storageKey))[storageKey]||activeData;
  if(current.knowledgeTerms?.sourceTranscriptSha256===result.sourceTranscriptSha256){
    const byKey=new Map(result.terms.map(term=>[term.normalizedTerm,term.wikipedia]));
    for(const term of current.knowledgeTerms.terms||[])if(byKey.has(term.normalizedTerm))term.wikipedia=byKey.get(term.normalizedTerm);
    await chrome.storage.local.set({[storageKey]:current});
  }
}

async function ensurePriorityTermDetail(index) {
  const term=activeData?.knowledgeTerms?.terms?.[index];
  if(!term||term.wikipedia?.status!=="not_queried")return;
  const videoId=activeVideoId,sourceHash=activeData.knowledgeTerms.sourceTranscriptSha256;
  const lookupKey=`${videoId}:${term.normalizedTerm}`;
  if(priorityLookups.has(lookupKey))return priorityLookups.get(lookupKey);
  const task=(async()=>{
    const reference=await resolveTermReference(term,activeData.knowledgeTerms.wikipediaLanguage||"en");
    if(activeVideoId!==videoId||activeData?.knowledgeTerms?.sourceTranscriptSha256!==sourceHash)return;
    const liveTerm=activeData.knowledgeTerms.terms[index];
    if(liveTerm?.normalizedTerm!==term.normalizedTerm)return;
    liveTerm.wikipedia=reference;renderKnowledgeTerms();
    const storageKey=PREFIX+videoId,current=(await chrome.storage.local.get(storageKey))[storageKey];
    const storedTerm=current?.knowledgeTerms?.terms?.find(item=>item.normalizedTerm===term.normalizedTerm);
    if(storedTerm&&current.knowledgeTerms.sourceTranscriptSha256===sourceHash){storedTerm.wikipedia=reference;await chrome.storage.local.set({[storageKey]:current})}
  })().finally(()=>priorityLookups.delete(lookupKey));
  priorityLookups.set(lookupKey,task);return task;
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
  const detailSlot=document.createElement("div");detailSlot.className="termDetailSlot";
  const selectedIndex=result.terms.findIndex((_,index)=>expandedTerms.has(`${activeVideoId}:${index}`));
  if(selectedIndex>=0){
    const term=result.terms[selectedIndex],detail=document.createElement("article");detail.className="termCard termDetail";
    const head=document.createElement("div");head.className="termHead";
    const name=document.createElement("span");name.className="termName";name.textContent=term.term;
    const score=document.createElement("span");score.className="termScore";score.textContent=`${Math.round(term.score)} 分`;
    head.append(name,score);
    const meta=document.createElement("div");meta.className="termMeta";
    meta.textContent=`${term.type}・出現 ${term.frequency} 次・跨 ${term.segmentCount} 段`;
    const explanation=document.createElement("div");explanation.className="termExplanation";
    const wiki=term.wikipedia||{status:"not_queried"};
    explanation.textContent=wiki.status==="not_queried"?"正在查詢 Wikipedia…":(wiki.explanation||"此詞條沒有可用的簡介。");
    const links=document.createElement("div");links.className="termLinks";
    if(wiki.url){const a=document.createElement("a");a.className="wikiLink";a.href=wiki.url;a.target="_blank";a.rel="noopener";a.textContent=wiki.status==="google_fallback"?"Google 搜尋":`${wiki.language}.wikipedia`;links.append(a)}
    for(const occurrence of (term.occurrences||[])){const button=document.createElement("button");button.className="termTime";button.textContent=fmtTime(occurrence.start);button.dataset.seconds=occurrence.start;links.append(button)}
    detail.append(head,meta,explanation,links);detailSlot.append(detail);
  }else{
    const placeholder=document.createElement("div");placeholder.className="termDetailPlaceholder";
    placeholder.textContent="點選下方專有名詞，或播放影片以查看目前提及的詞條。";detailSlot.append(placeholder);
  }
  const cloud = document.createElement("div");
  cloud.className = "termCloud";
  const limit = showAllTerms ? result.terms.length : 60;
  result.terms.forEach((term, index) => {
    const selected = index === selectedIndex;
    const playing = playingTermIndexes.has(index);
    if (index >= limit && !selected && !playing) return;
    const tag = document.createElement("button");
    tag.className = "termTag";
    tag.dataset.termIndex = String(index);
    tag.textContent = term.term;
    tag.classList.toggle("selected", selected);
    tag.classList.toggle("playing", playing);
    cloud.append(tag);
  });
  target.append(detailSlot, cloud);
  const resolved=result.terms.filter(term=>term.wikipedia?.status && term.wikipedia.status!=="not_queried").length;
  $("termStatus").textContent=`已找出 ${result.terms.length} 個候選；已預載 ${resolved} 個詞條`;
  $("moreTermsBtn").hidden = limit >= result.terms.length;
}

async function getActiveTabInfo() {
  const tabs = await chrome.tabs.query({active:true,currentWindow:true});
  const tab = tabs[0];
  if (!tab?.id || !tab.url?.includes("youtube.com/watch")) return null;
  return new Promise(resolve => {
    chrome.tabs.sendMessage(tab.id,{type:"YT_GET_VIDEO_INFO"},res=>resolve(chrome.runtime.lastError?null:(res?{...res,tabId:tab.id}:null)));
  });
}

let tickerRenderKey;
async function renderTickers(data) {
  const target = $("tickerResults");
  const transcript = data?.transcript;
  const key = JSON.stringify([data?.video?.videoId, transcript?.contentSha256, transcript?.capturedAt]);
  if (key === tickerRenderKey) return;
  tickerRenderKey = key;
  target.textContent = "正在比對台美股資料…";
  try {
    const tickers = globalThis.YTKITickers;
    const snapshot = await tickers.getSnapshot();
    if (key !== tickerRenderKey) return;
    const matches = tickers.match(snapshot, transcript?.segments || []);
    target.textContent = matches.length ? "" : "目前字幕沒有符合條件的股票。";
    if (!matches.length) return;
    const cloud = document.createElement("div");
    cloud.className = "termCloud";
    const detail = document.createElement("div");
    target.append(cloud, detail);
    for (const row of matches) {
      const item = document.createElement("article");
      item.className = "termCard";
      const summary = document.createElement("strong");
      summary.textContent = `${row.market} ${row.symbol} · ${row.name}`;
      const description = document.createElement("p");
      description.textContent = row.description || "可從下方官方資料查看公司業務。";
      const links = document.createElement("div");
      links.className = "termLinks";
      function addLink(label, value) {
        if (typeof value !== "string" || !value.trim()) return;
        try {
          const raw = value.trim();
          const url = new URL(/^[\w+.-]+:/.test(raw) ? raw : `https://${raw}`);
          if (!["https:", "http:"].includes(url.protocol) || !url.hostname.includes(".") || url.username || url.password) return;
          const link = document.createElement("a");
          link.href = url.href;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.textContent = label;
          links.append(link);
        } catch (_) {}
      }
      const isTaiwan = ["TWSE", "TPEx"].includes(row.market) && /^\d{4}$/.test(row.symbol);
      addLink("公司網站", row.website || (isTaiwan && row.url !== "https://mops.twse.com.tw/" ? row.url : null));
      addLink("官方公司簡介", row.profileUrl);
      if (isTaiwan) {
        addLink("MOPS 個股資料", `https://mops.twse.com.tw/mops/#/web/t146sb05?companyId=${row.symbol}`);
      } else {
        addLink("SEC 個股申報", row.url);
      }
      const evidence = document.createElement("p");
      evidence.textContent = `字幕命中：${row.terms.join("、")}`;
      item.append(summary, description, links, evidence);
      const tag = document.createElement("button");
      tag.className = "termTag";
      tag.textContent = `${row.symbol} · ${row.name}`;
      tag.setAttribute("aria-pressed", "false");
      tag.addEventListener("click", () => {
        const wasSelected = tag.classList.contains("selected");
        for (const button of cloud.children) {
          button.classList.remove("selected");
          button.setAttribute("aria-pressed", "false");
        }
        detail.replaceChildren();
        if (wasSelected) return;
        tag.classList.add("selected");
        tag.setAttribute("aria-pressed", "true");
        detail.append(item);
      });
      cloud.append(tag);
    }
  } catch (_) {
    if (key === tickerRenderKey) target.textContent = "暫時無法讀取股票資料。";
  }
}

async function loadData() {
  const generation=++loadGeneration;
  const info = await getActiveTabInfo();
  if(generation!==loadGeneration)return;
  if (!info?.videoId) {
    void renderTickers(null);
    $("videoTitle").textContent = "請先開啟 YouTube 影片";
    $("status").textContent = "未偵測到 YouTube 影片";
    return;
  }
  activeVideoId = info.videoId;
  activeTabId = info.tabId;
  observedTabKey=`${activeTabId}:${activeVideoId}`;
  if (lastTermVideoId !== activeVideoId) { expandedTerms.clear(); showAllTerms=false; playingTermIndexes.clear(); preloadKey=""; lastTermVideoId=activeVideoId; }
  $("videoTitle").textContent = info.title || info.videoId;
  const key = PREFIX + activeVideoId;
  const stored = await chrome.storage.local.get(key);
  if(generation!==loadGeneration)return;
  activeData = stored[key] || {
    schemaVersion:4, video:{videoId:activeVideoId,title:info.title,url:info.url},
    transcript:{sourceType:"none",completeness:"not_captured",segments:[]}, liveObservations:[]
  };
  const transcript=activeData.transcript||{};
  void renderTickers(activeData);
  const effective=transcript.language||"未知語言", original=transcript.originalLanguage||effective;
  const typeLabel=transcript.trackType==="ASR"?"（自動生成）":transcript.trackType==="manual"?"（創作者字幕）":"";
  $("transcriptSourceLabel").textContent=transcript.translationLanguage&&transcript.translationLanguage!==original
    ? `${languageLabel(original)}${typeLabel} → ${languageLabel(transcript.translationLanguage)}`
    : transcript.segments?.length ? `${languageLabel(effective)}${typeLabel}` : "等待逐字稿";
  $("diagnostics").textContent = activeData.captureDiagnostics?.latest
    ? JSON.stringify(activeData.captureDiagnostics.latest, null, 2)
    : "尚無診斷資料";
  renderKnowledgeTerms();
  ensureLocalKnowledgeTerms().catch(error => { $("termStatus").textContent=`本機分析失敗：${String(error?.message||error)}`; });
  if(activeData.knowledgeTerms?.method==="rule_based_v8_categorized_entities")preloadKnowledgeDetails().catch(error=>{$("termStatus").textContent=`詞條預載失敗：${String(error?.message||error)}`});
  const status = activeData.transcript?.completeness || "等待自動擷取";
  if (activeData.lastCapture?.detail) {
    showCaptureStatus(activeData.lastCapture.status, activeData.lastCapture.detail);
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
    showCaptureStatus(status, display[status] || status);
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
  expandedTerms.clear();expandedTerms.add(keyId);renderKnowledgeTerms();
  const term=activeData.knowledgeTerms.terms[index];
  if(!term||term.wikipedia?.status!=="not_queried")return;
  try{await ensurePriorityTermDetail(index)}catch(error){
    term.wikipedia={status:"lookup_error",language:null,explanation:`查詢失敗：${String(error?.message||error)}`,
      url:`https://www.google.com/search?q=${encodeURIComponent(term.term)}`};
    renderKnowledgeTerms();
  }
});

$("moreTermsBtn").addEventListener("click",()=>{showAllTerms=true;renderKnowledgeTerms()});

$("refreshBtn").addEventListener("click", async () => {
  const info = await getActiveTabInfo();
  if (!info?.videoId) return;
  $("status").textContent = "正在重新檢查…";
  chrome.tabs.query({active:true,currentWindow:true}, tabs => {
    if (tabs[0]?.id) chrome.tabs.sendMessage(tabs[0].id,{type:"YTKI_RECAPTURE",videoId:info.videoId},()=>setTimeout(loadData,1200));
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
    showCaptureStatus(msg.status, msg.detail);
    setTimeout(loadData,400);
  }
});
function showCaptureStatus(status, detail) {
  const node = $("status");
  node.title = detail || status;
  if (/partial/.test(status)) node.textContent = "部分字幕";
  else if (/complete|reaches_video_end/.test(status)) node.textContent = "已擷取";
  else if (/error|unavailable|not_loaded|empty/.test(status)) node.textContent = "擷取未完成";
  else node.textContent = "字幕檢查中";
}
function scheduleTabReload(){
  clearTimeout(tabReloadTimer);
  tabReloadTimer=setTimeout(()=>{
    loadGeneration++;
    activeVideoId="";activeData=null;expandedTerms.clear();playingTermIndexes.clear();preloadKey="";showAllTerms=false;
    $("videoTitle").textContent="正在切換影片…";
    $("transcriptSourceLabel").textContent="目前處理逐字稿：正在偵測新分頁";
    $("termList").className="terms empty";$("termList").textContent="正在載入此影片的專有名詞…";
    loadData();
  },150);
}
chrome.tabs.onActivated?.addListener(scheduleTabReload);
chrome.tabs.onUpdated?.addListener((tabId,changeInfo,tab)=>{
  if(tab?.active&&(changeInfo.url||changeInfo.status==="complete"))scheduleTabReload();
});
async function monitorActiveTab(){
  const tabs=await chrome.tabs.query({active:true,currentWindow:true}),tab=tabs[0];
  if(!tab?.id||!tab.url?.includes("youtube.com/watch"))return;
  let videoId="";try{videoId=new URL(tab.url).searchParams.get("v")||""}catch(_){}
  const key=`${tab.id}:${videoId}`;
  if(key!==observedTabKey){observedTabKey=key;scheduleTabReload()}
}
async function syncPlaybackTerm(){
  if(!activeVideoId||!activeData?.knowledgeTerms?.terms?.length)return;
  const tabs=await chrome.tabs.query({active:true,currentWindow:true}),tab=tabs[0];if(!tab?.id)return;
  const state=await new Promise(resolve=>chrome.tabs.sendMessage(tab.id,{type:"YTKI_GET_PLAYBACK_TIME"},res=>resolve(chrome.runtime.lastError?null:res)));
  if(state?.videoId&&state.videoId!==activeVideoId){observedTabKey=`${tab.id}:${state.videoId}`;scheduleTabReload();return}
  if(!state||state.paused||!Number.isFinite(state.currentTime))return;
  const now=state.currentTime;
  const active=[];
  activeData.knowledgeTerms.terms.forEach((term,index)=>(term.occurrences||[]).forEach(occ=>{
    const start=Number(occ.start),end=Number(occ.end),effectiveEnd=Number.isFinite(end)&&end>start?end:start+6;
    if(Number.isFinite(start)&&now>=start&&now<=effectiveEnd)active.push({index,start});
  }));
  const nextSet=new Set(active.map(item=>item.index));
  if(nextSet.size===playingTermIndexes.size&&[...nextSet].every(index=>playingTermIndexes.has(index)))return;
  playingTermIndexes=nextSet;
  if(active.length){
    const index=active.sort((a,b)=>Math.abs(now-a.start)-Math.abs(now-b.start))[0].index;
    expandedTerms.clear();expandedTerms.add(`${activeVideoId}:${index}`)
    for(const item of active)ensurePriorityTermDetail(item.index).catch(()=>{});
  }
  renderKnowledgeTerms();
}
setInterval(()=>syncPlaybackTerm().catch(()=>{}),800);
setInterval(()=>monitorActiveTab().catch(()=>{}),500);
loadData();
