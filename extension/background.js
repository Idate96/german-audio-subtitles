(() => {
  "use strict";
  const SERVICE_URL = "http://127.0.0.1:8766";
  const validToken = value => typeof value === "string" && /^[A-Za-z0-9_-]{32,128}$/.test(value);
  const evidence = new globalThis.GermanMediaEvidence.MediaEvidence();
  let enabled = true;
  let serviceToken = "";
  let lastStatus = {state: "waiting", message: "Play a Disney+ title with German audio to start."};
  const inflight = new Map();
  const ready = browser.storage.local.get({enabled: true, serviceToken: ""}).then(saved => {
    enabled = saved.enabled !== false;
    serviceToken = validToken(saved.serviceToken) ? saved.serviceToken : "";
    updateButton();
  });

  function isDisney(url) {
    try {
      const u = new URL(url);
      return u.protocol === "https:" && (u.hostname === "disneyplus.com" || u.hostname.endsWith(".disneyplus.com"));
    } catch { return false; }
  }

  function updateButton() {
    browser.browserAction.setBadgeText({text: enabled ? (serviceToken ? "DE" : "?") : ""});
    browser.browserAction.setBadgeBackgroundColor({color: "#183f35"});
    browser.browserAction.setTitle({title: enabled ? "German audio subtitles: on" : "German audio subtitles: off"});
  }

  function popupState(extra = {}) { return {enabled, paired: Boolean(serviceToken), status: lastStatus, ...extra}; }
  function unavailable(message) {
    lastStatus = {state: "offline", message};
    return {enabled, cues: [], status: lastStatus};
  }
  function pairingNeeded() {
    lastStatus = {state: "pairing", message: "Connect your local subtitle service using its pairing code."};
    return {enabled, cues: [], status: lastStatus};
  }
  function stalePlayback() {
    return {enabled, stale: true, cues: [], status: {state: "preparing", message: "Preparing subtitles for the current title."}};
  }

  async function playbackContext(sender) {
    try {
      // sender.url can retain an old Disney SPA document URL. The current tab
      // URL/title are authoritative; never attach a new movie to the old route.
      const tab = await browser.tabs.get(sender.tab.id);
      if (!isDisney(tab.url)) return null;
      const url = new URL(tab.url);
      const pageUrl = url.origin + url.pathname;
      const title = String(tab.title || "").slice(0, 250);
      const state = evidence.navigate(tab.id, pageUrl, false, title);
      return {tab, pageUrl, title, identity: pageUrl + "\n" + title, generation: state.generation};
    } catch { return null; }
  }

  async function stillCurrent(sender, captured) {
    const current = await playbackContext(sender);
    return current && current.identity === captured.identity && current.generation === captured.generation;
  }

  async function mediaClock(sender, videoIndex, action = "read") {
    if (!browser.scripting?.executeScript) return {mseTimestampOffset: null, mseClockState: "unavailable"};
    try {
      const result = await browser.scripting.executeScript({
        target: {tabId: sender.tab.id, frameIds: [sender.frameId || 0]}, world: "MAIN",
        func: globalThis.CocoPageMediaClock, args: [Number.isInteger(videoIndex) ? videoIndex : 0, action]
      });
      const observation = result[0]?.result;
      return {
        mseTimestampOffset: Number.isFinite(observation?.mseTimestampOffset) ? observation.mseTimestampOffset : null,
        mseClockState: String(observation?.mseClockState || "unavailable").slice(0, 30),
        mseMatchingBuffers: Number.isFinite(observation?.mseMatchingBuffers) ? observation.mseMatchingBuffers : 0,
        mseAssociation: String(observation?.mseAssociation || "").slice(0, 30),
        mseBuffers: Array.isArray(observation?.mseBuffers) ? observation.mseBuffers.slice(0, 8) : []
      };
    } catch { return {mseTimestampOffset: null, mseClockState: "unavailable"}; }
  }

  async function sync(message, sender) {
    await ready;
    if (!enabled) return {enabled: false, cues: [], status: {state: "disabled"}};
    if (!serviceToken) return pairingNeeded();
    if (!Number.isFinite(message.currentTime) || message.currentTime < 0) {
      return {enabled, cues: [], status: {state: "waiting", message: "Waiting for playback."}};
    }
    const captured = await playbackContext(sender);
    if (!captured) return stalePlayback();
    const diagnostics = globalThis.CocoSubtitleCore.sanitizeDiagnostics(message.diagnostics);
    const clock = await mediaClock(sender, diagnostics?.selectedVideoIndex);
    if (!await stillCurrent(sender, captured)) return stalePlayback();
    const body = {
      currentTime: message.currentTime,
      paused: message.paused !== false,
      playbackRate: Number.isFinite(message.playbackRate) ? message.playbackRate : 1,
      duration: Number.isFinite(message.duration) ? message.duration : null,
      title: captured.title,
      pageUrl: captured.pageUrl,
      diagnostics: {
        ...globalThis.CocoSubtitleCore.sanitizeDiagnostics({...diagnostics, ...clock}),
        mediaRequests: evidence.forTab(sender.tab.id, captured.pageUrl, captured.title)
      }
    };
    const key = `${sender.tab.id}:${sender.frameId || 0}`;
    if (inflight.has(key)) return {enabled, busy: true};
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 6000);
    inflight.set(key, controller);
    try {
      const response = await fetch(`${SERVICE_URL}/sync`, {
        method: "POST", headers: {"Content-Type": "application/json", "Authorization": `Bearer ${serviceToken}`},
        body: JSON.stringify(body), signal: controller.signal,
        credentials: "omit", redirect: "error", cache: "no-store"
      });
      if (!await stillCurrent(sender, captured)) return stalePlayback();
      if (!response.ok) return unavailable(response.status === 401
        ? "The pairing code was rejected. Reconnect the local service."
        : "The subtitle service is temporarily unavailable.");
      const data = await response.json();
      if (!await stillCurrent(sender, captured)) return stalePlayback();
      lastStatus = {
        state: String(data.status?.state || "waiting").slice(0, 30),
        message: String(data.status?.message || "").slice(0, 300),
        bufferedUntil: data.status?.bufferedUntil,
        transcribedUntil: data.status?.transcribedUntil
      };
      return {enabled, cues: Array.isArray(data.cues) ? data.cues.slice(0, 2000) : [], status: lastStatus};
    } catch {
      if (!await stillCurrent(sender, captured)) return stalePlayback();
      return unavailable("Start the local subtitle service to see German captions.");
    } finally {
      clearTimeout(timeout);
      inflight.delete(key);
    }
  }

  async function pair(value) {
    await ready;
    const token = typeof value === "string" ? value.trim() : "";
    if (!validToken(token)) return popupState({pairingError: "Paste the complete pairing code from your local setup."});
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 6000);
    try {
      const response = await fetch(`${SERVICE_URL}/status`, {
        headers: {"Authorization": `Bearer ${token}`}, signal: controller.signal,
        credentials: "omit", redirect: "error", cache: "no-store"
      });
      if (!response.ok) return popupState({pairingError: response.status === 401
        ? "That pairing code was not accepted." : "The local service could not connect. Try again."});
      serviceToken = token;
      await browser.storage.local.set({serviceToken});
      updateButton();
      lastStatus = {state: "preparing", message: "Connected. Play a Disney+ title with German audio."};
      return popupState({pairingSaved: true});
    } catch { return popupState({pairingError: "Start the local subtitle service, then connect again."}); }
    finally { clearTimeout(timeout); }
  }

  browser.runtime.onMessage.addListener((message, sender) => {
    if (sender.id === browser.runtime.id && sender.url === browser.runtime.getURL("popup.html")) {
      if (message?.type === "COCO_POPUP_SETTINGS") return ready.then(() => popupState());
      if (message?.type === "COCO_SET_ENABLED" && typeof message.enabled === "boolean") {
        return setEnabled(message.enabled).then(() => popupState());
      }
      if (message?.type === "COCO_RETRY") return retry().then(() => popupState());
      if (message?.type === "GERMAN_PAIR_SERVICE") return pair(message.code);
      return undefined;
    }
    if (sender.id !== browser.runtime.id || !sender.tab || !isDisney(sender.url)) return undefined;
    if (message?.type === "COCO_SYNC") return sync(message, sender);
    if (message?.type === "COCO_SETTINGS") return ready.then(() => ({enabled}));
    return undefined;
  });

  async function setEnabled(value) {
    await ready;
    enabled = value;
    await browser.storage.local.set({enabled});
    updateButton();
    if (!enabled) for (const request of inflight.values()) request.abort();
    const tabs = await browser.tabs.query({url: "https://*.disneyplus.com/*"});
    for (const tab of tabs) {
      browser.tabs.sendMessage(tab.id, {type: "COCO_ENABLED_CHANGED", enabled}).catch(() => {});
      if (!enabled) void mediaClock({tab, frameId: 0}, 0, "stop");
    }
  }

  async function retry() {
    await ready;
    if (!serviceToken) return pairingNeeded();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 6000);
    try {
      const response = await fetch(`${SERVICE_URL}/retry`, {
        method: "POST", headers: {"Content-Type": "application/json", "Authorization": `Bearer ${serviceToken}`},
        body: "{}", signal: controller.signal, credentials: "omit", redirect: "error", cache: "no-store"
      });
      if (!response.ok) return unavailable("The subtitle service could not reconnect.");
      lastStatus = {state: "preparing", message: "Retry requested. Return to your movie to continue."};
      return {enabled, status: lastStatus};
    } catch { return unavailable("Start the local subtitle service, then retry."); }
    finally { clearTimeout(timeout); }
  }

  const mediaFilter = {urls: ["https://*.media.dssott.com/*"]};
  browser.webRequest.onBeforeRequest.addListener(details => evidence.begin(details), mediaFilter);
  browser.webRequest.onCompleted.addListener(details => evidence.complete(details), mediaFilter, ["responseHeaders"]);
  browser.webRequest.onErrorOccurred.addListener(details => evidence.failed(details), mediaFilter);
  browser.tabs.onRemoved.addListener(tabId => evidence.remove(tabId));
  browser.tabs.onUpdated.addListener((tabId, change, tab) => {
    if (!isDisney(tab.url) && !evidence.tabs.has(tabId)) return;
    const prior = evidence.tabs.get(tabId)?.generation;
    const next = evidence.navigate(tabId, tab.url, change.status === "loading", tab.title);
    if (prior !== undefined && next?.generation !== prior) {
      for (const [key, request] of inflight) if (key.startsWith(`${tabId}:`)) request.abort();
    }
  });

  async function injectExistingTabs() {
    const tabs = await browser.tabs.query({url: "https://*.disneyplus.com/*"});
    for (const tab of tabs) {
      try {
        evidence.navigate(tab.id, tab.url, false, tab.title);
        await browser.tabs.executeScript(tab.id, {file: "subtitle-core.js"});
        await browser.tabs.executeScript(tab.id, {file: "content.js"});
      } catch { /* A tab can close or navigate while the extension is loading. */ }
    }
  }
  browser.runtime.onInstalled.addListener(injectExistingTabs);
  browser.runtime.onStartup.addListener(injectExistingTabs);
})();
