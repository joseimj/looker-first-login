/**
 * @fileoverview Splash screen UI (plain DOM, zero framework dependencies).
 * All user-visible strings live in MESSAGES for straightforward localization.
 */

import {State} from './flow.js';

const MESSAGES = {
  en: {
    title: 'Preparing your workspace',
    identifying: 'Checking your account…',
    provisioning: 'Setting up your access. This usually takes a few seconds…',
    provisioningRetry: 'Still finishing setup (check {n})…',
    redirecting: 'All set. Opening your dashboards…',
    ready: 'Your access is ready.',
    noAccess: 'Your account has no BI dashboards assigned yet. ' +
        'Please request access through your IT / identity portal.',
    requestAccess: 'Request BI access',
    failed: 'Workspace setup is taking longer than expected. ' +
        'Please try again in a moment.',
    retry: 'Try again',
    continueReload: 'Continue (page reload)',
    continueSpa: 'Continue (in-app navigation)',
    continueLogout: 'Re-authenticate (/logout)',
    continueNewTab: 'Open in new tab',
    details: 'Details',
  },
};

const STYLE = `
  body { margin: 0; font-family: Roboto, 'Helvetica Neue', Arial, sans-serif;
         color: #262d33; background: #f5f6f7; }
  .splash { min-height: 100vh; display: flex; flex-direction: column;
            align-items: center; justify-content: center; gap: 16px;
            padding: 24px; box-sizing: border-box; text-align: center; }
  .splash h1 { font-size: 24px; font-weight: 500; margin: 0; }
  .splash p { font-size: 16px; margin: 0; max-width: 480px; }
  .spinner { width: 40px; height: 40px; border-radius: 50%;
             border: 4px solid #dee1e5; border-top-color: #1a73e8;
             animation: spin 1s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  @media (prefers-reduced-motion: reduce) { .spinner { animation: none; } }
  .actions { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; }
  button { font: inherit; padding: 8px 16px; border-radius: 4px;
           border: 1px solid #1a73e8; background: #1a73e8; color: #fff;
           cursor: pointer; }
  details { font-size: 12px; color: #5f6368; max-width: 640px;
            text-align: start; }
  details pre { white-space: pre-wrap; margin: 8px 0 0; }
`;

function messagesFor(locale) {
  const language = String(locale || '').toLowerCase().split('-')[0];
  return MESSAGES[language] || MESSAGES.en;
}

export function createUi(root, locale, {accessRequestUrl = ''} = {}) {
  const text = messagesFor(locale);
  const doc = root.ownerDocument;
  const make = (tag, className) => {
    const element = doc.createElement(tag);
    if (className) element.className = className;
    return element;
  };

  const style = make('style');
  style.textContent = STYLE;
  const splash = make('main', 'splash');
  const title = make('h1');
  title.textContent = text.title;
  const spinner = make('div', 'spinner');
  spinner.setAttribute('aria-hidden', 'true');
  const status = make('p');
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  const actions = make('div', 'actions');
  const details = make('details');
  const summary = make('summary');
  summary.textContent = text.details;
  const logLines = make('pre');
  details.append(summary, logLines);
  splash.append(title, spinner, status, actions, details);
  root.replaceChildren(style, splash);

  let retryHandler = () => {};
  let continueHandler = () => {};
  let requestAccessHandler = () => {};
  const startedAt = Date.now();

  const showActions = (buttons) => {
    actions.replaceChildren(...buttons.map(([label, handler]) => {
      const button = make('button');
      button.type = 'button';
      button.textContent = label;
      button.addEventListener('click', handler);
      return button;
    }));
  };

  return {
    setState(state, info = {}) {
      const busy = state === State.IDENTIFYING ||
          state === State.PROVISIONING || state === State.REDIRECTING;
      spinner.hidden = !busy;
      showActions([]);
      switch (state) {
        case State.IDENTIFYING:
          status.textContent = text.identifying;
          break;
        case State.PROVISIONING:
          status.textContent = info.attempt > 1 ?
              text.provisioningRetry.replace('{n}', String(info.attempt)) :
              text.provisioning;
          break;
        case State.REDIRECTING:
          status.textContent = text.redirecting;
          break;
        case State.READY:
          status.textContent = text.ready;
          showActions([
            [text.continueReload, () => continueHandler('reload')],
            [text.continueSpa, () => continueHandler('spa')],
            [text.continueLogout, () => continueHandler('logout')],
            [text.continueNewTab, () => continueHandler('new_tab')],
          ]);
          break;
        case State.NO_ACCESS:
          status.textContent = text.noAccess;
          if (accessRequestUrl) {
            showActions([[text.requestAccess, () => requestAccessHandler()]]);
          }
          break;
        default:
          status.textContent = text.failed;
          showActions([[text.retry, () => retryHandler()]]);
      }
    },
    log(message) {
      const seconds = ((Date.now() - startedAt) / 1000).toFixed(1);
      logLines.textContent += `[+${seconds}s] ${message}\n`;
    },
    onRetry(handler) {
      retryHandler = handler;
    },
    onContinue(handler) {
      continueHandler = handler;
    },
    onRequestAccess(handler) {
      requestAccessHandler = handler;
    },
  };
}
