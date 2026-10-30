const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function contentHarness() {
  let interval;
  let animation;
  const sent = [];
  const elements = [];
  class Element {
    constructor() {
      this.style = {setProperty() {}};
      this.textContent = '';
      this.className = '';
      this.listeners = new Map();
      this.isConnected = true;
    }
    setAttribute() {}
    attachShadow() { return new Element(); }
    append(...children) { for (const child of children) child.parentNode = this; }
    remove() { this.parentNode = null; }
    addEventListener(name, fn) { this.listeners.set(name, fn); }
    removeEventListener(name) { this.listeners.delete(name); }
    getBoundingClientRect() { return {left: 0, top: 0, width: 1280, height: 720}; }
  }
  const video = new Element();
  Object.assign(video, {
    currentTime: 20, duration: 6300, paused: true, playbackRate: 1, currentSrc: 'blob:old',
    buffered: {length: 1, start: () => 0, end: () => 100},
    seekable: {length: 1, start: () => 0, end: () => 6300}
  });
  const document = {
    title: 'First movie | Disney+', fullscreenElement: null, documentElement: new Element(),
    createElement() { const element = new Element(); elements.push(element); return element; },
    querySelectorAll(selector) { return selector === 'video' ? [video] : []; }, addEventListener() {}
  };
  const location = {origin: 'https://www.disneyplus.com', pathname: '/de-de/play/first'};
  const context = vm.createContext({
    document, location, window: {addEventListener() {}},
    getComputedStyle: () => ({visibility: 'visible'}),
    requestAnimationFrame(fn) { animation = fn; return 1; }, cancelAnimationFrame() {},
    setInterval(fn) { interval = fn; return 1; }, clearInterval() {},
    browser: {runtime: {
      sendMessage(message) { return new Promise(resolve => sent.push({message, resolve})); },
      onMessage: {addListener() {}}
    }}
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../subtitle-core.js'), 'utf8'), context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../content.js'), 'utf8'), context);
  return {sent, document, location, video, poll: () => interval(), frame: () => animation(1000),
    caption: () => elements.find(element => element.className === 'caption').textContent};
}
const flush = () => new Promise(resolve => setImmediate(resolve));
const response = text => ({enabled: true, cues: [{start: 0, end: 100, text}], status: {state: 'ready'}});

test('overlay clears on SPA identity changes before an old asynchronous reply arrives', async () => {
  const harness = contentHarness();
  harness.sent[0].resolve(response('First movie words'));
  await flush();
  harness.frame();
  assert.equal(harness.caption(), 'First movie words');
  const pending = harness.poll();
  harness.location.pathname = '/de-de/play/second';
  harness.document.title = 'Second movie | Disney+';
  harness.frame();
  assert.equal(harness.caption(), '');
  harness.sent[1].resolve(response('Stale first movie words'));
  await pending;
  harness.frame();
  assert.equal(harness.caption(), '');
});

test('a source or title change also clears captions when the route has not updated yet', async () => {
  const harness = contentHarness();
  harness.sent[0].resolve(response('Old source words'));
  await flush();
  harness.frame();
  assert.equal(harness.caption(), 'Old source words');
  harness.video.currentSrc = 'blob:new-pipeline';
  harness.frame();
  assert.equal(harness.caption(), '');
  const pending = harness.poll();
  harness.sent[1].resolve(response('New source words'));
  await pending;
  harness.frame();
  assert.equal(harness.caption(), 'New source words');
  harness.document.title = 'New title before route update | Disney+';
  harness.frame();
  assert.equal(harness.caption(), '');
});
