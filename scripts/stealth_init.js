/* TPS stealth init script — injected into every page before any site JS runs
 * (scrapling `init_script` -> playwright add_init_script).
 * Kills only the classic, cheap-to-check automation tells. Nothing here can
 * solve a real challenge; it just stops volunteering "I am a bot". */
(() => {
  'use strict';
  try {
    // 1. navigator.webdriver — the single most-checked flag.
    Object.defineProperty(window.Navigator.prototype, 'webdriver', {
      get: () => false, configurable: true,
    });
    try { Object.defineProperty(navigator, 'webdriver', { get: () => false, configurable: true }); } catch (e) {}
  } catch (e) {}
  try {
    // 2. window.chrome stub — real Chromium always has it, headless automation often lacks runtime.
    if (!window.chrome) { window.chrome = {}; }
    if (!window.chrome.runtime) { window.chrome.runtime = {}; }
  } catch (e) {}
  try {
    // 3. Plugins & mimeTypes — headless default is empty, real browsers never are.
    const fakePlugin = (name, desc) => ({
      name, description: desc || '', filename: name.toLowerCase().replace(/\s+/g, '') + '.dll',
      length: 1, item: () => ({ type: 'application/x-' + name, suffixes: '', description: desc || '' }),
      namedItem: () => null,
    });
    const plugins = [
      fakePlugin('Chrome PDF Plugin', 'Portable Document Format'),
      fakePlugin('Chrome PDF Viewer'),
      fakePlugin('Native Client'),
    ];
    plugins.item = (i) => plugins[i] || null;
    plugins.namedItem = (n) => plugins.find((p) => p.name === n) || null;
    Object.defineProperty(navigator, 'plugins', { get: () => plugins, configurable: true });
    Object.defineProperty(navigator, 'mimeTypes', {
      get: () => ({ length: 3, item: () => null, namedItem: () => null }), configurable: true,
    });
  } catch (e) {}
  try {
    // 4. Languages — must agree with the Accept-Language header (en-US).
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'], configurable: true });
    Object.defineProperty(navigator, 'language', { get: () => 'en-US', configurable: true });
  } catch (e) {}
  try {
    // 5. Permissions query for Notifications — headless answers "denied" promptly, real Chrome says "default"/prompt.
    const origQuery = window.navigator.permissions && window.navigator.permissions.query;
    if (origQuery) {
      window.navigator.permissions.__tps_orig_query = origQuery;
      window.navigator.permissions.query = (params) =>
        params && params.name === 'notifications'
          ? Promise.resolve({ state: 'default' })
          : origQuery(params);
    }
  } catch (e) {}
  try {
    // 6. Hardware concurrency / device memory — headless defaults (often 8/8) are fine,
    // but make them stable desktop values instead of leaking container sizing.
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8, configurable: true });
    Object.defineProperty(navigator, 'deviceMemory', { get: () => 8, configurable: true });
  } catch (e) {}
})();
