/**
 * @fileoverview Bundles the extension into the single dist/bundle.js file
 * that the LookML manifest points at, and configures the local dev server.
 *
 * @looker/extension-sdk (and its transitive dependencies) references Node's
 * `process` and `Buffer` globals, which Webpack 5 does not polyfill by
 * default. Both fallbacks and ProvidePlugin shims are wired below.
 */

const path = require('path');
const webpack = require('webpack');

module.exports = {
  mode: 'production',
  entry: './src/index.js',
  output: {
    filename: 'bundle.js',
    path: path.resolve(__dirname, 'dist'),
  },
  module: {
    rules: [
      // Lets ESM dependencies import extensionless modules such as
      // `process/browser` under Webpack 5.
      {test: /\.m?js$/, resolve: {fullySpecified: false}},
    ],
  },
  resolve: {
    fallback: {
      process: require.resolve('process/browser'),
      buffer: require.resolve('buffer/'),
    },
  },
  plugins: [
    new webpack.ProvidePlugin({
      process: 'process/browser',
      Buffer: ['buffer', 'Buffer'],
    }),
  ],
  devServer: {
    port: 8080,
    // Looker runs extensions inside a sandboxed iframe with an opaque origin;
    // disabling HMR/liveReload avoids WebSocket errors in the browser console.
    hot: false,
    liveReload: false,
    allowedHosts: 'all',
    headers: {
      'Access-Control-Allow-Origin': '*',
    },
  },
  performance: {hints: false},
};
