/**
 * @fileoverview Unit tests of the splash screen, run against a minimal
 * stand-in for the browser DOM (plain Node, no dependencies).
 */

import assert from 'node:assert/strict';
import {test} from 'node:test';

import {createUi} from '../src/ui.js';

/** The few DOM element features the splash screen relies on. */
class FakeElement {
  constructor(tag, ownerDocument) {
    this.tagName = tag;
    this.ownerDocument = ownerDocument;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.textContent = '';
    this.hidden = false;
  }
  setAttribute(name, value) {
    this.attributes[name] = value;
  }
  append(...children) {
    this.children.push(...children);
  }
  replaceChildren(...children) {
    this.children = children;
  }
  addEventListener(type, handler) {
    this.listeners[type] = handler;
  }
  click() {
    this.listeners.click();
  }
  /** Every descendant with the given tag, in document order. */
  all(tag) {
    return this.children.flatMap(
        (child) => [...(child.tagName === tag ? [child] : []),
                    ...child.all(tag)]);
  }
}

/** Builds the splash screen in a fake page and exposes its parts. */
function makeScreen(locale) {
  const doc = {};
  doc.createElement = (tag) => new FakeElement(tag, doc);
  const root = new FakeElement('body', doc);
  const ui = createUi(root, locale);
  return {
    ui,
    title: () => root.all('h1')[0].textContent,
    status: () => root.all('p')[0].textContent,
    spinnerHidden: () => root.all('div')[0].hidden,
    buttons: () => root.all('button'),
    labels: () => root.all('button').map((button) => button.textContent),
    log: () => root.all('pre')[0].textContent,
  };
}

test('shows the title and a spinner while provisioning', () => {
  const screen = makeScreen('en-US');

  screen.ui.setState('provisioning', {attempt: 1});

  assert.equal(screen.title(), 'Preparing your workspace');
  assert.equal(
      screen.status(), 'Setting up your access. This takes a few seconds…');
  assert.equal(screen.spinnerHidden(), false);
  assert.deepEqual(screen.labels(), []);
});

test('mentions the attempt number on later attempts', () => {
  const screen = makeScreen('en');

  screen.ui.setState('provisioning', {attempt: 3});

  assert.equal(screen.status(), 'Still working on it (attempt 3)…');
});

test('announces the redirect', () => {
  const screen = makeScreen('en');

  screen.ui.setState('redirecting', {target: '/dashboards/12'});

  assert.equal(screen.status(), 'All set. Opening your dashboards…');
  assert.equal(screen.spinnerHidden(), false);
});

test('offers a retry when provisioning failed', () => {
  const screen = makeScreen('en');
  let retries = 0;
  screen.ui.onRetry(() => {
    retries++;
  });

  screen.ui.setState('failed', {error: new Error('boom')});
  screen.buttons()[0].click();

  assert.equal(
      screen.status(),
      'We could not finish setting up your access. ' +
          'Please try again in a moment.');
  assert.equal(screen.spinnerHidden(), true);
  assert.deepEqual(screen.labels(), ['Try again']);
  assert.equal(retries, 1);
});

test('offers all three redirect modes when access is ready', () => {
  const screen = makeScreen('en');
  const modes = [];
  screen.ui.onContinue((mode) => {
    modes.push(mode);
  });

  screen.ui.setState('ready', {target: '/dashboards/12'});
  screen.buttons()[0].click();
  screen.buttons()[1].click();
  screen.buttons()[2].click();

  assert.equal(screen.status(), 'Your access is ready.');
  assert.deepEqual(
      screen.labels(),
      [
        'Continue (page reload)',
        'Continue (in-app navigation)',
        'Open in new tab',
      ]);
  assert.deepEqual(modes, ['reload', 'spa', 'new_tab']);
});

test('tells a user without entitlements whom to contact', () => {
  const screen = makeScreen('en');

  screen.ui.setState('no_access');

  assert.equal(
      screen.status(),
      'Your account has no dashboards assigned yet. ' +
          'Please contact your BI support team.');
  assert.equal(screen.spinnerHidden(), true);
  assert.deepEqual(screen.labels(), []);
});

test('removes the buttons when leaving a state that had some', () => {
  const screen = makeScreen('en');
  screen.ui.setState('failed', {error: new Error('boom')});

  screen.ui.setState('provisioning', {attempt: 1});

  assert.deepEqual(screen.labels(), []);
});

test('keeps a timestamped log of what happened', () => {
  const screen = makeScreen('en');

  screen.ui.log('Connected to the Looker host');
  screen.ui.log('Backend answered "provisioned" in 812 ms (attempt 1)');

  assert.match(
      screen.log(),
      /^\[\+\d+\.\ds\] Connected to the Looker host\n\[\+\d+\.\ds\] Backend answered "provisioned" in 812 ms \(attempt 1\)\n$/);
});

test('falls back to English for a language without translation', () => {
  const screen = makeScreen('xx-YY');

  screen.ui.setState('identifying');

  assert.equal(screen.status(), 'Checking your account…');
});
