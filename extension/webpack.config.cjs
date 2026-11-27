/**
 * @fileoverview Bundles the extension into dist/bundle.js and configures
 * the local dev server with Webpack 5 polyfills for @looker/extension-sdk.
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
    hot: false,
    liveReload: false,
    allowedHosts: 'all',
    headers: {'Access-Control-Allow-Origin': '*'},
  },
  performance: {hints: false},
};
