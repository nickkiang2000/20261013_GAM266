// Runs in YouTube's MAIN world. Keep this deliberately small: it reads only
// the caption-track metadata already supplied to the player and never handles
// extension secrets or sends data off the page.
(() => {
  if (window.__ytki03BridgeInstalled) return;
  window.__ytki03BridgeInstalled = true;

  const EVENT_REQUEST = "ytki03:request-caption-tracks";
  const EVENT_RESPONSE = "ytki03:caption-tracks";

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

  function reply() {
    window.postMessage({ type: EVENT_RESPONSE, tracks: tracksFromPlayer() }, location.origin);
  }

  window.addEventListener("message", event => {
    if (event.source === window && event.origin === location.origin && event.data?.type === EVENT_REQUEST) reply();
  });
  document.addEventListener("yt-navigate-finish", () => setTimeout(reply, 250));
})();
