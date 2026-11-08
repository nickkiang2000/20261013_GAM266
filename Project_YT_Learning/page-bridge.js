// Runs in YouTube's MAIN world. Keep this deliberately small: it reads only
// the caption-track metadata already supplied to the player and never handles
// extension secrets or sends data off the page.
(() => {
  if (window.__ytki03BridgeInstalled) return;
  window.__ytki03BridgeInstalled = true;

  const EVENT_REQUEST = "ytki03:request-caption-tracks";
  const EVENT_RESPONSE = "ytki03:caption-tracks";
  const MAX_BODY_LENGTH = 8 * 1024 * 1024;
  const capturedPayloads = [];

  function timedTextInfo(value) {
    try {
      const url = new URL(typeof value === "string" ? value : value?.url || "", location.href);
      if (url.protocol !== "https:" || !(url.hostname === "youtube.com" || url.hostname.endsWith(".youtube.com")) || url.pathname !== "/api/timedtext") return null;
      const partialKeys = ["range", "sq", "rn", "start", "end", "begin"];
      return {
        videoId: url.searchParams.get("v") || "",
        languageCode: url.searchParams.get("lang") || "",
        translationLanguageCode: url.searchParams.get("tlang") || "",
        format: url.searchParams.get("fmt") || "unknown",
        requestScope: partialKeys.some(key => url.searchParams.has(key)) ? "partial" : "whole_track",
        queryKeys: [...new Set([...url.searchParams.keys()])].filter(key => !/sig|key|token|pot|expire/i.test(key)).slice(0, 40)
      };
    } catch (_) {
      return null;
    }
  }

  function rememberPayload(info, body, source) {
    if (!info || typeof body !== "string" || !body.trim() || body.length > MAX_BODY_LENGTH) return;
    const signature = `${info.videoId}|${info.languageCode}|${info.translationLanguageCode}|${info.format}|${body.length}|${body.slice(0, 32)}|${body.slice(-32)}`;
    const duplicateIndex=capturedPayloads.findIndex(item=>item.signature===signature);
    if(duplicateIndex>=0){
      // Repeated requests for the already-active track are common while the
      // player runs. Only notify again when an older payload becomes active,
      // which represents switching back to a previous language/translation.
      if(duplicateIndex===capturedPayloads.length-1)return;
      const existing=capturedPayloads.splice(duplicateIndex,1)[0];
      existing.capturedAt=new Date().toISOString();
      existing.source=source;
      capturedPayloads.push(existing);
      reply();
      return;
    }
    capturedPayloads.push({
      signature,
      videoId: info.videoId,
      languageCode: info.languageCode,
      translationLanguageCode: info.translationLanguageCode,
      format: info.format,
      requestScope: info.requestScope,
      queryKeys: info.queryKeys,
      body,
      source,
      capturedAt: new Date().toISOString()
    });
    while (capturedPayloads.length > 12) capturedPayloads.shift();
    reply();
  }

  // Observe the caption response YouTube's own player already requested. The
  // original Request/Response objects are returned untouched; parsing happens
  // only on a clone or after XHR load.
  const nativeFetch = window.fetch;
  if (typeof nativeFetch === "function") {
    window.fetch = async function(...args) {
      const info = timedTextInfo(args[0]);
      const response = await nativeFetch.apply(this, args);
      if (info) response.clone().text().then(body => rememberPayload(info, body, "player_fetch_clone")).catch(() => {});
      return response;
    };
  }

  const xhrUrl = Symbol("ytki03TimedTextUrl");
  const nativeOpen = XMLHttpRequest.prototype.open;
  const nativeSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url, ...rest) {
    this[xhrUrl] = timedTextInfo(url);
    return nativeOpen.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function(...args) {
    if (this[xhrUrl]) {
      this.addEventListener("load", () => {
        try {
          if (!this.responseType || this.responseType === "text") rememberPayload(this[xhrUrl], this.responseText, "player_xhr_response");
        } catch (_) {}
      }, { once: true });
    }
    return nativeSend.apply(this, args);
  };

  function playerResponses() {
    const player = document.getElementById("movie_player") || document.querySelector("#movie_player");
    const app = document.querySelector("ytd-app");
    const candidates = [
      player?.getPlayerResponse?.(),
      window.ytInitialPlayerResponse,
      window.ytplayer?.config?.args?.player_response,
      app?.data?.playerResponse,
      app?.data?.response?.playerResponse
    ];
    return candidates.map(value => {
      if (typeof value === "string") {
        try { return JSON.parse(value); } catch (_) { return null; }
      }
      return value;
    }).filter(Boolean);
  }

  function tracksFromPlayer() {
    for (const response of playerResponses()) {
      const tracks = response?.captions?.playerCaptionsTracklistRenderer?.captionTracks;
      if (Array.isArray(tracks) && tracks.length) {
        return tracks.map(track => ({
          baseUrl: track.baseUrl || "",
          languageCode: track.languageCode || "",
          kind: track.kind || null,
          vssId: track.vssId || "",
          name: track.name?.simpleText || track.name?.runs?.map(run => run.text).join("") || "",
          isTranslatable: !!track.isTranslatable
        }));
      }
    }
    return [];
  }

  function durationFromPlayer() {
    for (const response of playerResponses()) {
      const value = Number(response?.videoDetails?.lengthSeconds);
      if (Number.isFinite(value) && value > 0) return value;
    }
    return null;
  }

  function reply() {
    window.postMessage({
      type: EVENT_RESPONSE,
      tracks: tracksFromPlayer(),
      playerDurationSeconds: durationFromPlayer(),
      payloads: capturedPayloads.map(({ signature, ...payload }) => payload)
    }, location.origin);
  }

  window.addEventListener("message", event => {
    if (event.source === window && event.origin === location.origin && event.data?.type === EVENT_REQUEST) reply();
  });
  document.addEventListener("yt-navigate-finish", () => setTimeout(reply, 250));
})();
