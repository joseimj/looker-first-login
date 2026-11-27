/**
 * @fileoverview Unit tests for the splash screen UI (plain Node, fake DOM).
 */

import assert from 'node:assert/strict';
import {test} from 'node:test';

import {createUi} from '../src/ui.js';

class FakeElement {
  constructor(tag, doc) {
    this.tagName = tag;
    this.ownerDocument = doc;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.textContent = '';
    this.hidden = false;
  }
  setAttribute(k, v) { this.attributes[k] = v; }
  append(...c) { this.children.push(...c); }
  replaceChildren(...c) { this.children = c; }
  addEventListener(t, h) { this.listeners[t] = h; }
  click() { this.listeners.click(); }
  all(tag) {
    return this.children.flatMap(
        (c) => [...(c.tagName === tag ? [c] : []), ...c.all(tag)]);
  }
}

function makeScreen(opts) {
  const doc = {};
  doc.createElement = (t) => new FakeElement(t, doc);
  const root = new FakeElement('body', doc);
  const ui = createUi(root, 'en-US', opts);
  return {
    ui,
    status: () => root.all('p')[0].textContent,
    buttons: () => root.all('button'),
    labels: () => root.all('button').map((b) => b.textContent),
  };
}

test('ready state exposes reload, spa, logout, and new_tab diagnostic buttons', () => {
  const s = makeScreen();
  const clicked = [];
  s.ui.onContinue((m) => clicked.push(m));
  s.ui.setState('ready', {target: '/browse'});
  s.buttons().forEach((b) => b.click());

  assert.deepEqual(clicked, ['reload', 'spa', 'logout', 'new_tab']);
});

test('no_access shows self-service IGA button when accessRequestUrl is configured', () => {
  const s = makeScreen({accessRequestUrl: 'https://iga.example.com/request'});
  let opened = 0;
  s.ui.onRequestAccess(() => { opened++; });
  s.ui.setState('no_access');
  s.buttons()[0].click();

  assert.deepEqual(s.labels(), ['Request BI access']);
  assert.equal(opened, 1);
});
