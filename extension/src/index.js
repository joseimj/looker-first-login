/**
 * @fileoverview Entry point connecting the splash screen to the Looker host.
 */

import {connectExtensionHost, LookerExtensionSDK} from '@looker/extension-sdk';

import {CONFIG} from './config.js';
import {extractNextParam, redirect, runProvisioning, State} from './flow.js';
import {createUi} from './ui.js';

function start() {
  const ui = createUi(document.body, navigator.language, {
    accessRequestUrl: CONFIG.accessRequestUrl,
  });
  let extensionSDK;
  let sdk;
  let initialRoute;

  const run = async () => {
    try {
      if (!extensionSDK) {
        extensionSDK = await connectExtensionHost({
          setInitialRoute: (route) => {
            initialRoute = route;
          },
        });
        const createClient = LookerExtensionSDK.create40Client ||
            LookerExtensionSDK.createClient;
        sdk = createClient.call(LookerExtensionSDK, extensionSDK);
        ui.log(`Connected to Looker host (mode=${CONFIG.mode})`);
        if (CONFIG.accessRequestUrl) {
          ui.onRequestAccess(() => {
            extensionSDK.openBrowserWindow(CONFIG.accessRequestUrl, '_blank');
          });
        }
      }
      const nextPath = extractNextParam(initialRoute);
      const outcome = await runProvisioning({
        sdk,
        extensionSDK,
        ui,
        config: CONFIG,
        nextPath,
      });
      if (outcome.state === State.READY) {
        ui.onContinue((mode) => {
          ui.log(`Redirecting to ${outcome.target} (${mode})`);
          redirect(extensionSDK, outcome.target, mode);
        });
      }
    } catch (error) {
      ui.log(`Unexpected error: ${error && error.message}`);
      ui.setState(State.FAILED, {error});
    }
  };

  ui.onRetry(run);
  run();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', start);
} else {
  start();
}
