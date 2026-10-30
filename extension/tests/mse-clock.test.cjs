const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function setup() {
  let cleanup;
  class TimeRanges {
    constructor(ranges) { this.ranges = ranges; }
    get length() { return this.ranges.length; }
    start(i) { return this.ranges[i][0]; }
    end(i) { return this.ranges[i][1]; }
  }
  class HTMLMediaElement {
    constructor(time) { this.time = time; this.isSeeking = false; this.src = "blob:existing"; this.listeners = new Map(); }
    get currentTime() { return this.time; }
    get seeking() { return this.isSeeking; }
    get currentSrc() { return this.src; }
    addEventListener(name, fn) { this.listeners.set(name, fn); }
    removeEventListener(name) { this.listeners.delete(name); }
  }
  class SourceBuffer {
    constructor(offset, ranges = [[24, 74]]) {
      this.offset = offset; this.ranges = ranges; this.bufferMode = 'segments'; this.calls = [];
    }
    get timestampOffset() { return this.offset; }
    get buffered() { return new TimeRanges(this.ranges); }
    get mode() { return this.bufferMode; }
    appendBuffer(...args) {
      if (this.failure) throw this.failure;
      this.calls.push(args);
      return 'native-result';
    }
  }
  class MediaSource {
    constructor() { this.buffers = []; }
    addSourceBuffer(mime) {
      const buffer = new SourceBuffer(0);
      this.buffers.push(buffer);
      return buffer;
    }
    get sourceBuffers() { return this.buffers; }
    get activeSourceBuffers() { return this.buffers; }
  }
  let urlCounter = 0;
  const URL = {createObjectURL: () => `blob:test-${++urlCounter}`};
  const video = new HTMLMediaElement(38.485);
  const window = {SourceBuffer, TimeRanges, HTMLMediaElement, MediaSource, URL};
  const context = vm.createContext({
    window, document: {querySelectorAll: () => [video]}, WeakRef, performance: {now: () => 1000},
    setTimeout(fn) { cleanup = fn; return 1; }, clearTimeout() {}
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../mse-clock.js'), 'utf8'), context);
  return {observe: context.CocoPageMediaClock, SourceBuffer, MediaSource, URL, video, window, expire: () => cleanup()};
}

test('observer preserves native arguments, return values and exceptions without reading bytes', () => {
  const {observe, SourceBuffer} = setup();
  assert.equal(observe(0).mseTimestampOffset, null);
  const buffer = new SourceBuffer(-800);
  const bytes = new Proxy({}, {get() { throw new Error('Audio bytes must not be inspected'); }});
  assert.equal(buffer.appendBuffer(bytes), 'native-result');
  assert.equal(buffer.calls[0][0], bytes);
  assert.equal(observe(0).mseTimestampOffset, -800);
  const nativeFailure = new TypeError('native error');
  buffer.failure = nativeFailure;
  assert.throws(() => buffer.appendBuffer(bytes), error => error === nativeFailure);
  observe(0, 'stop');
});

test('offset consensus rejects mismatched buffers and ignores unrelated timelines', () => {
  const {observe, SourceBuffer} = setup();
  observe(0);
  const audio = new SourceBuffer(-800);
  const video = new SourceBuffer(-801);
  const unrelated = new SourceBuffer(-1000, [[100, 200]]);
  audio.appendBuffer({}); video.appendBuffer({}); unrelated.appendBuffer({});
  assert.equal(observe(0).mseClockState, 'ambiguous');
  assert.equal(observe(0).mseTimestampOffset, null);
  video.offset = -800;
  video.appendBuffer({});
  assert.equal(observe(0).mseTimestampOffset, -800);
  assert.equal(observe(0).mseMatchingBuffers, 2);
  observe(0, 'stop');
});

test('seeks, unobserved offset changes and sequence-mode streams cannot supply an offset', () => {
  const {observe, SourceBuffer, video} = setup();
  observe(0);
  const buffer = new SourceBuffer(-800);
  buffer.appendBuffer({});
  video.isSeeking = true;
  assert.equal(observe(0).mseTimestampOffset, null);
  video.isSeeking = false;
  buffer.offset = -820;
  assert.equal(observe(0).mseTimestampOffset, null);
  buffer.appendBuffer({});
  assert.equal(observe(0).mseTimestampOffset, -820);
  buffer.bufferMode = 'sequence';
  assert.equal(observe(0).mseTimestampOffset, null);
  observe(0, 'stop');
});

test('repeated installation does not stack wrappers; idle cleanup restores the original method', () => {
  const {observe, SourceBuffer, window, expire} = setup();
  const original = SourceBuffer.prototype.appendBuffer;
  observe(0);
  const wrapper = SourceBuffer.prototype.appendBuffer;
  assert.notEqual(wrapper, original);
  observe(0);
  assert.equal(SourceBuffer.prototype.appendBuffer, wrapper);
  expire();
  assert.equal(SourceBuffer.prototype.appendBuffer, original);
  assert.equal(Object.hasOwn(window, '__germanAudioMSEClock_v2'), false);
});

test('cleanup does not replace a newer wrapper installed by the page', () => {
  const {observe, SourceBuffer} = setup();
  observe(0);
  const newer = function newerPageWrapper() {};
  SourceBuffer.prototype.appendBuffer = newer;
  observe(0, 'stop');
  assert.equal(SourceBuffer.prototype.appendBuffer, newer);
});

test('a forward seek ignores retained buffers from the previous MediaSource', () => {
  const {observe, MediaSource, URL, video} = setup();
  observe(0);
  const oldSource = new MediaSource();
  const oldAudio = oldSource.addSourceBuffer('audio/mp4; codecs="mp4a.40.2"');
  video.src = URL.createObjectURL(oldSource);
  oldAudio.offset = -800;
  oldAudio.appendBuffer({});
  assert.equal(observe(0).mseTimestampOffset, -800);

  const newSource = new MediaSource();
  const newAudio = newSource.addSourceBuffer('audio/mp4; codecs="mp4a.40.2"');
  video.src = URL.createObjectURL(newSource);
  newAudio.offset = -4442;
  newAudio.appendBuffer({});
  oldAudio.appendBuffer({}); // A retained hidden pipeline still overlaps raw time.
  const result = observe(0);
  assert.equal(result.mseTimestampOffset, -4442);
  assert.equal(result.mseAssociation, 'active-source');
  assert.equal(result.mseMatchingBuffers, 1);
  assert.ok(result.mseBuffers.some(item => item.association === 'other-source' && item.offset === -800));
  observe(0, 'stop');
});

test('identified audio wins over a legitimate different video timestamp offset', () => {
  const {observe, MediaSource, URL, video} = setup();
  observe(0);
  const source = new MediaSource();
  const audio = source.addSourceBuffer('audio/mp4; codecs="mp4a.40.2"');
  const picture = source.addSourceBuffer('video/mp4; codecs="avc1.640028"');
  video.src = URL.createObjectURL(source);
  audio.offset = -4442.125;
  picture.offset = -4442.139;
  audio.appendBuffer({}); picture.appendBuffer({});
  const result = observe(0);
  assert.equal(result.mseTimestampOffset, -4442.125);
  assert.equal(result.mseMatchingBuffers, 1);
  assert.equal(result.mseBuffers.filter(item => item.eligible).length, 2);
  observe(0, 'stop');
});

test('late installation resets changed sources but preserves buffered rewinds on the same source', () => {
  const {observe, SourceBuffer, video} = setup();
  observe(0);
  const old = new SourceBuffer(-800);
  old.appendBuffer({});
  assert.equal(observe(0).mseTimestampOffset, -800);
  video.src = 'blob:created-before-hook';
  assert.equal(observe(0).mseTimestampOffset, null);
  const current = new SourceBuffer(-4442);
  current.appendBuffer({});
  assert.equal(observe(0).mseTimestampOffset, -4442);
  video.listeners.get('seeking')();
  video.time = 30; // Already buffered; no new append is required.
  assert.equal(observe(0).mseTimestampOffset, -4442);
  observe(0, 'stop');
});

test('untyped audio/video offsets within two milliseconds are a usable consensus', () => {
  const {observe, SourceBuffer} = setup();
  observe(0);
  const first = new SourceBuffer(-5192.000123);
  const second = new SourceBuffer(-5192.000456);
  first.appendBuffer({}); second.appendBuffer({});
  assert.equal(observe(0).mseClockState, 'observed');
  assert.equal(observe(0).mseTimestampOffset, -5192.000123);
  second.offset = -5192.003;
  second.appendBuffer({});
  assert.equal(observe(0).mseTimestampOffset, null);
  observe(0, 'stop');
});
