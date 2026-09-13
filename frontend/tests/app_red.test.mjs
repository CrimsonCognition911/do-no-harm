import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {webcrypto} from 'node:crypto';
import vm from 'node:vm';
import test from 'node:test';
import {VoiceController, correlateActions} from '../voice_controller.mjs';

test('actual app closes microphone and WebRTC and requests pause on feed failure', async () => {
  const elements = new Map();
  const element = () => ({handlers: {}, disabled: false, value: '', checked: false,
    addEventListener(name, handler) {this.handlers[name] = handler;},
    append() {}, play: async () => {}, pause() {this.paused = true;}});
  const document = {body: {}, querySelector: id => {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  }, createElement: element};
  const requests = [];
  let failFeed = false, scheduled, stopped = false, closed = false, dataChannel;
  const track = {stop() {stopped = true;}};
  const microphone = {getTracks: () => [track]};
  class Peer {
    iceGatheringState = 'complete';
    createDataChannel() {
      dataChannel = {readyState: 'open', handlers: {}, addEventListener(n, h) {this.handlers[n] = h;}, send() {}, close() {this.readyState = 'closed';}};
      return dataChannel;
    }
    addEventListener() {}
    addTrack() {}
    createOffer = async () => ({type: 'offer', sdp: 'offer'});
    async setLocalDescription(value) {this.localDescription = value;}
    async setRemoteDescription() {dataChannel.handlers.message({data: JSON.stringify({type: 'session.started'})});}
    close() {closed = true;}
  }
  const context = vm.createContext({document, window: {addEventListener() {}}, VoiceController, correlateActions,
    crypto: webcrypto, RTCPeerConnection: Peer, navigator: {mediaDevices: {getUserMedia: async () => microphone}},
    setTimeout: callback => {scheduled = callback; return 1;}, clearTimeout() {},
    fetch: async (url, options) => {
      requests.push([url, options]);
      if (url.startsWith('/api/events') && failFeed) throw new Error('feed offline');
      const body = url === '/api/bootstrap' ? {csrf: 'local', live_available: true} :
        url.startsWith('/api/events') ? {state: 'running', execution_version: 1, events: [], next_cursor: 0} :
        url === '/api/live/session' ? {session: {id: 'live'}, transport: {sdp: 'answer'}} :
        url === '/api/technical-pause' ? {state: 'pause_requested', execution_version: 2} : {};
      return {ok: true, json: async () => body};
    }});
  const source = readFileSync(new URL('../app.js', import.meta.url), 'utf8').replace(/^import .*;\n/, '');
  await vm.runInContext(`(async () => {${source}\n})()`, context);
  await new Promise(resolve => setImmediate(resolve));
  elements.get('#consent').checked = true;
  await elements.get('#consent').handlers.change();
  await elements.get('#start').handlers.click();
  assert.equal(stopped, false);
  failFeed = true;
  await scheduled();
  assert.equal(stopped, true);
  assert.equal(closed, true);
  assert.equal(elements.get('#remote-audio').muted, true);
  assert.equal(elements.get('#remote-audio').srcObject, null);
  assert(requests.some(([url]) => url === '/api/technical-pause'));
  assert(requests.some(([url]) => url === '/api/live/hangup'));
  assert.equal(elements.get('#remote-audio').handlers.timeupdate, undefined);
});
