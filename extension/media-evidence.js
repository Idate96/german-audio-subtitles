(() => {
  "use strict";

  function playbackKey(url) {
    try {
      const parsed = new URL(url);
      if (parsed.protocol !== "https:" || !(parsed.hostname === "disneyplus.com" || parsed.hostname.endsWith(".disneyplus.com"))) return null;
      return parsed.pathname.match(/\/play\/([A-Za-z0-9-]{1,100})(?:\/|$)/)?.[1] || null;
    } catch { return null; }
  }

  function canonicalMediaPath(url) {
    try {
      const parsed = new URL(url);
      if (parsed.protocol !== "https:" || !parsed.hostname.endsWith(".media.dssott.com")) return null;
      const components = parsed.pathname.split("/").filter(Boolean);
      if (components[0]?.length >= 128) components.shift();
      const path = "/" + components.join("/");
      return path.length > 1 && path.length <= 1024 ? path : null;
    } catch { return null; }
  }

  class MediaEvidence {
    constructor() {
      this.tabs = new Map();
      this.pending = new Map();
      this.generation = 0;
    }

    navigate(tabId, url, fullNavigation = false, title = "", authoritative = true) {
      if (!Number.isInteger(tabId) || tabId < 0) return null;
      const key = playbackKey(url);
      title = typeof title === "string" ? title.slice(0, 250) : "";
      let state = this.tabs.get(tabId);
      if (!state || fullNavigation || state.key !== key || (state.title && title && state.title !== title)) {
        state = {key, title, authoritative, generation: ++this.generation, requests: new Map()};
        this.tabs.set(tabId, state);
        for (const [id, request] of this.pending) if (request.tabId === tabId) this.pending.delete(id);
      }
      if (title && !state.title) state.title = title;
      if (authoritative) state.authoritative = true;
      return state;
    }

    begin(details) {
      if (!Number.isInteger(details.tabId) || details.tabId < 0 || !canonicalMediaPath(details.url)) return;
      const documentKey = playbackKey(details.documentUrl || details.originUrl);
      let state = this.tabs.get(details.tabId);
      if (!state && documentKey) state = this.navigate(details.tabId, details.documentUrl || details.originUrl, false, "", false);
      // documentUrl may keep the original document URL through Disney SPA
      // navigation. tabs.get/onUpdated state wins; request generation fences
      // reject requests that actually began before the navigation.
      if (!state?.key || (!state.authoritative && documentKey && documentKey !== state.key)) return;
      this.pending.set(details.requestId, {tabId: details.tabId, generation: state.generation});
      if (this.pending.size > 512) this.pending.delete(this.pending.keys().next().value);
    }

    complete(details) {
      const started = this.pending.get(details.requestId);
      this.pending.delete(details.requestId);
      if (!started || started.tabId !== details.tabId || details.statusCode < 200 || details.statusCode >= 300) return;
      const state = this.tabs.get(details.tabId);
      if (!state?.key || state.generation !== started.generation) return;
      const documentKey = playbackKey(details.documentUrl || details.originUrl);
      if (!state.authoritative && documentKey && documentKey !== state.key) return;
      const header = details.responseHeaders?.find(item => item.name?.toLowerCase() === "content-type");
      const type = String(header?.value || "").toLowerCase().split(";", 1)[0].trim();
      if (!(type.startsWith("audio/") || type.includes("mpegurl"))) return;
      const path = canonicalMediaPath(details.url);
      if (!path) return;
      const observedAt = Number.isFinite(details.timeStamp) ? Math.round(details.timeStamp) : Date.now();
      state.requests.delete(path);
      state.requests.set(path, {path, type: type.slice(0, 100), observedAt});
      while (state.requests.size > 128) state.requests.delete(state.requests.keys().next().value);
    }

    failed(details) { this.pending.delete(details.requestId); }

    remove(tabId) {
      this.tabs.delete(tabId);
      for (const [id, request] of this.pending) if (request.tabId === tabId) this.pending.delete(id);
    }

    forTab(tabId, url, title = "") {
      const state = this.navigate(tabId, url, false, title);
      const result = state?.key ? [...state.requests.values()].map(item => ({...item})) : [];
      // Keep the complete sync request below the service's body limit, while
      // retaining up to128 useful paths across long pauses and buffered rewinds.
      while (JSON.stringify(result).length > 48000) result.shift();
      return result;
    }
  }

  globalThis.GermanMediaEvidence = Object.freeze({MediaEvidence, playbackKey, canonicalMediaPath});
})();
