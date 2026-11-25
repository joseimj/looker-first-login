/**
 * @fileoverview Entry point of the landing extension: connects to the Looker
 * host, then runs the provisioning flow behind the splash screen.
 */

import {connectExtensionHost, LookerExtensionSDK} from '@looker/extension-sdk';

import {CONFIG} from './config.js';
import {redirect, runProvisioning, State} from './flow.js';
import {createUi} from './ui.js';

function start() {
  const ui = createUi(document.body, navigator.language);
  let extensionSDK;
  let sdk;

  const run = async () => {
    try {
      if (!extensionSDK) {
        extensionSDK = await connectExtensionHost();
        // Named create40Client in older releases of the SDK.
        const createClient = LookerExtensionSDK.create40Client ||
            LookerExtensionSDK.createClient;
        sdk = createClient.call(LookerExtensionSDK, extensionSDK);
        ui.log('Connected to the Looker host');
      }
      const outcome =
          await runProvisioning({sdk, extensionSDK, ui, config: CONFIG});
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
