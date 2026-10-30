(() => {
  "use strict";

  function normalizeCues(input) {
    if (!Array.isArray(input)) return [];
    return input.slice(0, 2000).flatMap((cue) => {
      if (!cue || !Number.isFinite(cue.start) || !Number.isFinite(cue.end) ||
          cue.start < 0 || cue.end <= cue.start || typeof cue.text !== "string") return [];
      const text = cue.text.trim().slice(0, 2000);
      return text ? [{start: cue.start, end: cue.end, text}] : [];
    }).sort((a, b) => a.start - b.start || a.end - b.end);
  }

  function captionAt(cues, time) {
    if (!Number.isFinite(time)) return "";
    const lines = [];
    for (const cue of cues) {
      if (cue.start > time) break;
      if (cue.end > time && !lines.includes(cue.text)) lines.push(cue.text);
    }
    return lines.join("\n");
  }

  function sanitizeDiagnostics(input) {
    if (!input || typeof input !== "object") return undefined;
    const number = value => Number.isFinite(value) ? Math.round(value * 1000000) / 1000000 : null;
    const string = value => typeof value === "string" ? value.slice(0, 72) : "";
    const ranges = value => Array.isArray(value) ? value.slice(0, 4)
      .filter(pair => Array.isArray(pair) && pair.length === 2 && pair.every(Number.isFinite))
      .map(pair => pair.map(number)) : [];
    const controls = Array.isArray(input.controls) ? input.controls.slice(0, 6).map(item => {
      const clean = {};
      for (const key of ["tag", "role", "label", "valueText", "valueNow", "min", "max", "value", "ariaValueMax", "testId", "className"]) {
        if (item && typeof item[key] === "string" && item[key]) clean[key] = string(item[key]);
      }
      return clean;
    }) : [];
    const result = {
      currentSrcType: ["blob", "url", "empty"].includes(input.currentSrcType) ? input.currentSrcType : "empty",
      mseTimestampOffset: Number.isFinite(input.mseTimestampOffset) ? input.mseTimestampOffset : null,
      mseClockState: string(input.mseClockState),
      mseMatchingBuffers: number(input.mseMatchingBuffers),
      mseAssociation: string(input.mseAssociation),
      mseBuffers: Array.isArray(input.mseBuffers) ? input.mseBuffers.slice(0, 8).map(item => ({
        offset: number(item?.offset), appendOffset: number(item?.appendOffset),
        kind: string(item?.kind), mime: string(item?.mime), association: string(item?.association),
        ageMs: number(item?.ageMs), start: number(item?.start), end: number(item?.end), eligible: item?.eligible === true
      })) : [],
      videoCount: number(input.videoCount),
      selectedVideoIndex: number(input.selectedVideoIndex),
      buffered: ranges(input.buffered),
      seekable: ranges(input.seekable),
      videos: Array.isArray(input.videos) ? input.videos.slice(0, 5).map(item => ({
        currentTime: number(item?.currentTime), duration: number(item?.duration),
        paused: item?.paused !== false, area: number(item?.area)
      })) : [],
      controls,
      times: Array.isArray(input.times) ? input.times.slice(0, 8).map(item => ({
        tag: string(item?.tag), testId: string(item?.testId), className: string(item?.className), text: string(item?.text)
      })) : []
    };
    // Keep diagnostic requests small, even if site markup changes dramatically.
    while (JSON.stringify(result).length > 3000) {
      if (result.times.length) result.times.pop();
      else if (result.controls.length) result.controls.pop();
      else break;
    }
    return result;
  }

  globalThis.CocoSubtitleCore = Object.freeze({normalizeCues, captionAt, sanitizeDiagnostics});
})();
