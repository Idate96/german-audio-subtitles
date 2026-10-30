(() => {
  "use strict";

  // Runs in MAIN with no extension objects, tokens, network access or audio-byte
  // inspection. Captured native operations are always called unchanged.
  globalThis.CocoPageMediaClock = function observeMediaClock(videoIndex, action = "read") {
    const marker = "__germanAudioMSEClock_v2";
    const empty = state => ({mseTimestampOffset: null, mseClockState: state, mseMatchingBuffers: 0, mseBuffers: []});
    try {
      const legacy = Object.getOwnPropertyDescriptor(window, "__germanAudioMSEClock_v1")?.value;
      if (typeof legacy?.stop === "function") legacy.stop();
      const previous = Object.getOwnPropertyDescriptor(window, marker)?.value;
      if (action === "stop") {
        if (typeof previous?.stop === "function") previous.stop();
        return empty("stopped");
      }
      if (typeof previous?.read === "function") return previous.read(videoIndex);
      const proto = window.SourceBuffer?.prototype;
      const rangeProto = window.TimeRanges?.prototype;
      const mediaProto = window.HTMLMediaElement?.prototype;
      const sourceProto = window.MediaSource?.prototype;
      if (!proto || !rangeProto || !mediaProto) return empty("unavailable");
      const descriptor = (object, name) => object && Object.getOwnPropertyDescriptor(object, name);
      const getter = (object, name) => descriptor(object, name)?.get;
      const offsetGetter = getter(proto, "timestampOffset");
      const bufferedGetter = getter(proto, "buffered");
      const modeGetter = getter(proto, "mode");
      const lengthGetter = getter(rangeProto, "length");
      const timeGetter = getter(mediaProto, "currentTime");
      const seekingGetter = getter(mediaProto, "seeking");
      const srcGetter = getter(mediaProto, "currentSrc");
      const activeBuffersGetter = getter(sourceProto, "activeSourceBuffers");
      const sourceBuffersGetter = getter(sourceProto, "sourceBuffers");
      const audioTracksGetter = getter(proto, "audioTracks");
      const rangeStart = rangeProto.start;
      const rangeEnd = rangeProto.end;
      if ([offsetGetter, bufferedGetter, modeGetter, lengthGetter, timeGetter, seekingGetter, rangeStart, rangeEnd]
        .some(value => typeof value !== "function")) return empty("unavailable");

      const apply = Reflect.apply;
      const tracked = [];
      const metadata = new WeakMap();
      const urls = new Map();
      const restorations = [];
      let timer;
      let stopped = false;
      let selectedRef = null;
      let selectedSrc = "";
      let generation = 0;
      let seekListener = null;
      const now = () => performance.now();
      const get = (fn, object) => apply(fn, object, []);
      const getSrc = element => srcGetter ? get(srcGetter, element) : (element.currentSrc || "");

      function refreshContext(selected) {
        const src = getSrc(selected);
        if (selectedRef?.deref() !== selected || src !== selectedSrc) {
          const old = selectedRef?.deref();
          if (old && seekListener) old.removeEventListener?.("seeking", seekListener);
          selectedRef = new WeakRef(selected);
          selectedSrc = src;
          generation += 1;
          // Rewinding within the same MediaSource keeps its valid clock. Only
          // an actual source/video change invalidates late-install association.
          seekListener = () => refreshContext(selected);
          selected.addEventListener?.("seeking", seekListener);
        }
      }

      function recordFor(buffer) {
        let record = metadata.get(buffer);
        if (!record) {
          // Native getter also verifies that this is a real SourceBuffer.
          get(offsetGetter, buffer);
          record = {ref: new WeakRef(buffer), mime: "", source: null, offset: null, lastAppend: null, generation: -1};
          metadata.set(buffer, record);
          tracked.push(record);
          if (tracked.length > 32) tracked.shift();
        }
        return record;
      }

      function remember(buffer) {
        const selected = selectedRef?.deref();
        if (selected) refreshContext(selected);
        const record = recordFor(buffer);
        record.offset = get(offsetGetter, buffer);
        record.lastAppend = now();
        record.generation = generation;
      }

      function hook(object, name, after) {
        const original = descriptor(object, name);
        if (!original?.configurable || typeof original.value !== "function") return false;
        const call = original.value;
        function wrapper(...args) {
          const result = apply(call, this, args);
          try { if (!stopped) after(this, args, result); } catch { /* Never affect the player's operation. */ }
          return result;
        }
        Object.defineProperty(object, name, {...original, value: wrapper});
        restorations.push(() => {
          if (descriptor(object, name)?.value === wrapper) Object.defineProperty(object, name, original);
        });
        return true;
      }

      function stop() {
        if (stopped) return;
        stopped = true;
        clearTimeout(timer);
        for (const restore of restorations.reverse()) { try { restore(); } catch { /* Page may have replaced a descriptor. */ } }
        const selected = selectedRef?.deref();
        if (selected && seekListener) selected.removeEventListener?.("seeking", seekListener);
        tracked.length = 0;
        urls.clear();
        if (descriptor(window, marker)?.value === state) delete window[marker];
      }

      function rangesOf(buffer) {
        const ranges = get(bufferedGetter, buffer);
        const result = [];
        for (let i = 0, count = Math.min(get(lengthGetter, ranges), 32); i < count; i += 1) {
          result.push([apply(rangeStart, ranges, [i]), apply(rangeEnd, ranges, [i])]);
        }
        return result;
      }

      function kindOf(buffer, record) {
        if (/^audio\//i.test(record.mime)) return "audio";
        if (/^video\//i.test(record.mime)) return "video";
        try {
          if (audioTracksGetter && get(audioTracksGetter, buffer)?.length > 0) return "audio";
        } catch { /* Track APIs are optional. */ }
        return "unknown";
      }

      function read(index) {
        clearTimeout(timer);
        timer = setTimeout(stop, action === "start" && index === null ? 60000 : 15000);
        const videos = document.querySelectorAll("video");
        if (!Number.isInteger(index) || index < 0 || index >= videos.length) return empty("no-video");
        const selected = videos[index];
        refreshContext(selected);
        const time = get(timeGetter, selected);
        if (!Number.isFinite(time) || get(seekingGetter, selected)) return empty("seeking");
        const source = urls.get(selectedSrc)?.deref();
        let active = null;
        if (source) {
          try {
            const list = get(activeBuffersGetter || sourceBuffersGetter, source);
            active = new Set();
            for (let i = 0; i < Math.min(list.length, 32); i += 1) {
              const buffer = list[i];
              active.add(buffer);
              const record = recordFor(buffer);
              record.source = new WeakRef(source);
            }
          } catch { /* A closed/replaced MediaSource cannot confirm association. */ }
        }

        const candidates = [];
        const evidence = [];
        for (const record of tracked) {
          const buffer = record.ref.deref();
          if (!buffer) continue;
          try {
            const range = rangesOf(buffer).find(([start, end]) => start <= time && time < end);
            if (!range) continue;
            const offset = get(offsetGetter, buffer);
            const kind = kindOf(buffer, record);
            const recent = record.generation === generation && record.lastAppend !== null;
            const associated = active ? active.has(buffer) : recent;
            const association = active ? (active.has(buffer) ? "active-source" : "other-source") : (recent ? "fresh-context" : "stale-context");
            const valid = associated && Number.isFinite(offset) && get(modeGetter, buffer) === "segments" &&
              record.offset !== null && Math.abs(offset - record.offset) <= 0.000001;
            const item = {offset, appendOffset: record.offset, kind, mime: record.mime.slice(0, 100), association,
              ageMs: record.lastAppend === null ? null : Math.round(now() - record.lastAppend),
              start: range[0], end: range[1], eligible: valid};
            evidence.push(item);
            if (associated) candidates.push(item);
          } catch { /* Removed SourceBuffers are no longer useful. */ }
        }
        const audio = candidates.filter(item => item.kind === "audio");
        const chosen = audio.length ? audio : candidates;
        const report = {mseTimestampOffset: null, mseClockState: "waiting-for-append", mseMatchingBuffers: chosen.length,
          mseAssociation: active ? "active-source" : "fresh-context", mseBuffers: evidence.slice(-8)};
        if (!chosen.length) return report;
        if (chosen.some(item => !item.eligible)) return {...report, mseClockState: "ambiguous"};
        const offset = chosen[0].offset;
        if (chosen.some(item => Math.abs(item.offset - offset) > 0.002)) return {...report, mseClockState: "ambiguous"};
        return {...report, mseTimestampOffset: offset, mseClockState: "observed"};
      }

      const state = Object.freeze({read, stop});
      Object.defineProperty(window, marker, {value: state, configurable: true});
      try {
        if (!hook(proto, "appendBuffer", receiver => remember(receiver))) { stop(); return empty("unavailable"); }
        hook(sourceProto, "addSourceBuffer", (source, args, buffer) => {
          const record = recordFor(buffer);
          record.source = new WeakRef(source);
          record.mime = typeof args[0] === "string" ? args[0].slice(0, 160) : "";
        });
        hook(proto, "changeType", (buffer, args) => {
          recordFor(buffer).mime = typeof args[0] === "string" ? args[0].slice(0, 160) : "";
        });
        hook(window.URL, "createObjectURL", (_receiver, args, url) => {
          if (!sourceBuffersGetter || typeof url !== "string") return;
          get(sourceBuffersGetter, args[0]); // Brand-check only; never inspect Blob data.
          urls.set(url, new WeakRef(args[0]));
          if (urls.size > 32) urls.delete(urls.keys().next().value);
        });
      } catch { stop(); return empty("unavailable"); }
      return read(videoIndex);
    } catch { return empty("unavailable"); }
  };
})();
