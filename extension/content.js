(() => {
  "use strict";
  if (globalThis.__cocoSubtitlesLoaded) return;
  globalThis.__cocoSubtitlesLoaded = true;
  // Reloading a temporary extension destroys its old script context, but DOM
  // nodes can survive in the movie tab. Remove only this extension's hosts.
  for (const staleHost of document.querySelectorAll("[data-german-audio-subtitles]")) staleHost.remove();
  const {normalizeCues, captionAt, sanitizeDiagnostics} = globalThis.CocoSubtitleCore;

  let enabled = true;
  let video = null;
  let cues = [];
  let status = {state: "preparing"};
  let generation = 0;
  let pageKey = "";
  let mediaIdentity = "";
  let pending = false;
  let frame = null;
  let lastLayout = 0;
  let nativeTrack = null;
  let nativeCuesSignature = "";
  let disposed = false;
  const host = document.createElement("div");
  host.setAttribute("data-german-audio-subtitles", "");
  host.style.cssText = "all:initial!important;position:fixed!important;pointer-events:none!important;z-index:2147483647!important;display:none;";
  const shadow = host.attachShadow({mode: "closed"});
  const style = document.createElement("style");
  style.textContent = `
    :host { color-scheme: dark; }
    .caption-wrap { position:absolute; left:6%; right:6%; bottom:7%; text-align:center; }
    .caption { display:inline; padding:.14em .35em; color:white; background:rgba(0,0,0,.76);
      font-family:Arial,Helvetica,sans-serif; font-size:var(--caption-size,28px); font-weight:500;
      line-height:1.45; white-space:pre-line; box-decoration-break:clone;
      -webkit-box-decoration-break:clone; text-shadow:0 1px 3px #000; border-radius:.12em; }
    .caption:empty { display:none; }
    .status { position:absolute; top:4%; left:3%; max-width:75%; border-radius:5px;
      padding:5px 9px; color:#f2f3f2; background:rgba(0,0,0,.65); opacity:.82;
      font:13px/1.35 Arial,Helvetica,sans-serif; }
    .status:empty { display:none; }
  `;
  const wrap = document.createElement("div");
  wrap.className = "caption-wrap";
  const caption = document.createElement("span");
  caption.className = "caption";
  const indicator = document.createElement("div");
  indicator.className = "status";
  wrap.append(caption);
  shadow.append(style, wrap, indicator);

  function statusText() {
    if (!enabled || !video) return "";
    switch (status.state) {
      case "offline": return "German subtitles disconnected";
      case "error": return "German subtitles temporarily unavailable";
      case "unsupported": return "German subtitles are unavailable for this title";
      case "pairing": return "Connect German subtitles using the extension button";
      case "preparing":
      case "waiting":
        if (/\b(select|choose|switch)\b.*\bGerman\b/i.test(status.message || "")) return status.message.slice(0, 140);
        if (Number.isFinite(status.transcribedUntil) && status.transcribedUntil > video.currentTime) return "";
        return "Preparing German subtitles…";
      default: return "";
    }
  }

  function useNativeCaptions() {
    return video && document.fullscreenElement === video;
  }

  function refreshNativeTrack() {
    if (!video || !useNativeCaptions() || !enabled) {
      if (nativeTrack) nativeTrack.mode = "disabled";
      return;
    }
    if (!nativeTrack) nativeTrack = video.addTextTrack("subtitles", "German audio subtitles", "de");
    const signature = JSON.stringify(cues);
    if (signature !== nativeCuesSignature) {
      while (nativeTrack.cues?.length) nativeTrack.removeCue(nativeTrack.cues[0]);
      for (const cue of cues) nativeTrack.addCue(new VTTCue(cue.start, cue.end, cue.text));
      nativeCuesSignature = signature;
    }
    nativeTrack.mode = "showing";
  }

  function layout() {
    if (!video || !enabled || !video.isConnected || useNativeCaptions()) {
      host.style.setProperty("display", "none", "important");
      return;
    }
    const rect = video.getBoundingClientRect();
    if (rect.width < 80 || rect.height < 45) {
      host.style.setProperty("display", "none", "important");
      return;
    }
    const fullscreen = document.fullscreenElement;
    const parent = fullscreen?.contains(video) ? fullscreen : document.documentElement;
    if (host.parentNode !== parent) parent.append(host);
    host.style.setProperty("display", "block", "important");
    host.style.setProperty("left", `${rect.left}px`, "important");
    host.style.setProperty("top", `${rect.top}px`, "important");
    host.style.setProperty("width", `${rect.width}px`, "important");
    host.style.setProperty("height", `${rect.height}px`, "important");
    host.style.setProperty("--caption-size", `${Math.max(18, Math.min(40, rect.width * 0.025))}px`);
  }

  function playbackIdentity() {
    // Blob URLs stay inside this isolated script; only a change is relevant.
    return location.origin + location.pathname + "\n" + document.title + "\n" + (video?.currentSrc || "");
  }

  function refreshPlaybackIdentity() {
    const identity = playbackIdentity();
    if (identity !== mediaIdentity) {
      mediaIdentity = identity;
      cues = [];
      status = {state: "preparing"};
      generation += 1;
      refreshNativeTrack();
    }
    return identity;
  }

  function render() {
    refreshPlaybackIdentity();
    const text = video && enabled ? captionAt(cues, video.currentTime) : "";
    if (caption.textContent !== text) caption.textContent = text;
    const message = statusText();
    if (indicator.textContent !== message) indicator.textContent = message;
  }

  function tick(now) {
    if (disposed) return;
    render();
    if (now - lastLayout > 250) { layout(); lastLayout = now; }
    frame = requestAnimationFrame(tick);
  }

  function onSeek() {
    generation += 1;
    render();
    // A pending old response is discarded using generation; the next poll gets
    // the seek target. Never continue advancing the old subtitle timeline.
    void poll();
  }

  function onFullscreen() { refreshNativeTrack(); layout(); render(); }

  function attach(next) {
    if (next === video) return;
    if (video) {
      video.removeEventListener("seeking", onSeek);
      video.removeEventListener("timeupdate", render);
      video.removeEventListener("pause", render);
      video.removeEventListener("play", render);
    }
    if (nativeTrack) {
      nativeTrack.mode = "disabled";
      while (nativeTrack.cues?.length) nativeTrack.removeCue(nativeTrack.cues[0]);
    }
    nativeTrack = null;
    nativeCuesSignature = "";
    video = next;
    cues = [];
    status = {state: "preparing"};
    generation += 1;
    if (video) {
      video.addEventListener("seeking", onSeek);
      video.addEventListener("timeupdate", render);
      video.addEventListener("pause", render);
      video.addEventListener("play", render);
    }
    layout(); render();
  }

  function findVideo() {
    return [...document.querySelectorAll("video")].map((element) => {
      const rect = element.getBoundingClientRect();
      const visible = rect.width >= 160 && rect.height >= 90 &&
        getComputedStyle(element).visibility !== "hidden";
      return {element, area: visible ? rect.width * rect.height : 0};
    }).filter((item) => item.area > 0).sort((a, b) => b.area - a.area)[0]?.element || null;
  }

  function mediaDiagnostics() {
    const videos = [...document.querySelectorAll("video")];
    const ranges = (range) => {
      const result = [];
      if (!range) return result;
      for (let i = 0; i < Math.min(range.length, 4); i += 1) {
        try { result.push([range.start(i), range.end(i)]); } catch { break; }
      }
      return result;
    };
    const controls = [...document.querySelectorAll('[role="slider"],input[type="range"],progress')].slice(0, 6).map(element => ({
      tag: element.tagName.toLowerCase(), role: element.getAttribute("role"),
      label: element.getAttribute("aria-label"), valueText: element.getAttribute("aria-valuetext"),
      valueNow: element.getAttribute("aria-valuenow"), min: element.getAttribute("min"),
      max: element.getAttribute("max"), value: String(element.value ?? element.getAttribute("value") ?? ""),
      ariaValueMax: element.getAttribute("aria-valuemax"),
      testId: element.getAttribute("data-testid"), className: element.getAttribute("class")
    }));
    const times = [...document.querySelectorAll('[data-testid*="time" i],[data-testid*="duration" i],[class*="time" i],[class*="duration" i]')]
      .slice(0, 160).map(element => ({
        tag: element.tagName.toLowerCase(), testId: element.getAttribute("data-testid"),
        className: element.getAttribute("class"), text: element.textContent.trim().replace(/\s+/g, " ")
      })).filter(item => item.text.length <= 100 && /(?:^|[^\d])\d{1,2}:\d{2}(?::\d{2})?(?!\d)/.test(item.text)).slice(0, 8);
    return sanitizeDiagnostics({
      currentSrcType: video.currentSrc.startsWith("blob:") ? "blob" : video.currentSrc ? "url" : "empty",
      videoCount: videos.length, selectedVideoIndex: videos.indexOf(video),
      buffered: ranges(video.buffered), seekable: ranges(video.seekable),
      videos: videos.slice(0, 5).map(element => {
        const rect = element.getBoundingClientRect();
        return {currentTime: element.currentTime, duration: element.duration, paused: element.paused, area: rect.width * rect.height};
      }),
      controls, times
    });
  }

  async function poll() {
    if (disposed) return;
    const key = location.origin + location.pathname;
    if (key !== pageKey) {
      pageKey = key;
      cues = [];
      status = {state: "preparing"};
      generation += 1;
    }
    attach(findVideo());
    const requestIdentity = refreshPlaybackIdentity();
    if (!video || pending) return;
    const requestGeneration = generation;
    pending = true;
    try {
      const response = await browser.runtime.sendMessage({
        type: "COCO_SYNC", currentTime: video.currentTime, paused: video.paused,
        playbackRate: video.playbackRate, duration: video.duration,
        title: document.title, pageUrl: key, diagnostics: mediaDiagnostics()
      });
      if (disposed || requestGeneration !== generation || !response || response.busy) return;
      if (playbackIdentity() !== requestIdentity) { refreshPlaybackIdentity(); render(); return; }
      enabled = response.enabled !== false;
      status = response.status || {state: "waiting"};
      const receivedCues = normalizeCues(response.cues);
      if (!enabled || status.state === "unsupported") cues = [];
      else if (receivedCues.length || !["offline", "error"].includes(status.state)) cues = receivedCues;
      refreshNativeTrack(); layout(); render();
    } catch {
      status = {state: "offline"};
      render();
    } finally { pending = false; }
  }

  browser.runtime.onMessage.addListener((message) => {
    if (message?.type !== "COCO_ENABLED_CHANGED") return;
    enabled = message.enabled !== false;
    if (!enabled) cues = [];
    generation += 1;
    refreshNativeTrack(); layout(); render();
    void poll();
  });
  window.addEventListener("resize", layout);
  window.addEventListener("scroll", layout, true);
  document.addEventListener("fullscreenchange", onFullscreen);
  const timer = setInterval(poll, 1000);
  window.addEventListener("pagehide", (event) => {
    if (event.persisted) return;
    disposed = true;
    clearInterval(timer);
    if (frame !== null) cancelAnimationFrame(frame);
    attach(null);
    host.remove();
  });
  frame = requestAnimationFrame(tick);
  void poll();
})();
