// SmartestGuide — JS smoke test (běží PŘED deployem, lokálně)
// -------------------------------------------------------------
// HTTP smoke test (smoke_test.py) neumí spustit JavaScript, takže runtime
// chyby (Temporal Dead Zone, ReferenceError, SyntaxError) projdou nepovšimnuty
// až do prohlížeče. Tenhle check načte statické HTML v jsdom a SPUSTÍ jejich
// <script> — když něco hodí výjimku při inicializaci, skončí s chybou.
//
// Chytil by např. bug z 0.5.4: `let currentLang` deklarovaný pod inicializací
// kalkulačky → TDZ → shodí celý skript (mrtvý přepínač jazyků).
//
// Spuštění:
//   npm install jsdom
//   node js_smoke.js
//
// Návratový kód: 0 = vše OK, 1 = nalezena JS chyba (vhodné do CI před deploy).

const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

// Soubory s inline skripty, které se mají ověřit
const FILES = ['landing.html', 'index.html', 'hotel.html', 'guest.html'];

function checkFile(file) {
  const full = path.join(__dirname, file);
  if (!fs.existsSync(full)) return { skipped: true };
  const html = fs.readFileSync(full, 'utf8');
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => {
    const msg = (e.detail && (e.detail.message || e.detail.stack)) || e.message || String(e);
    errors.push(String(msg).split('\n')[0]);
  });
  try {
    new JSDOM(html, {
      runScripts: 'dangerously',
      pretendToBeVisual: true,
      virtualConsole: vc,
      beforeParse(window) {
        // Polyfilly pro browser API, která jsdom nemá — jinak falešné chyby.
        // NEPOLYFILUJEME nic, co by zamaskovalo skutečné chyby v naší logice.
        window.IntersectionObserver = class {
          constructor() {} observe() {} unobserve() {} disconnect() {} takeRecords() { return []; }
        };
        window.fetch = () => Promise.resolve({
          ok: true, status: 200,
          json: () => Promise.resolve({}),
          text: () => Promise.resolve(''),
          blob: () => Promise.resolve({ size: 0 }),
          headers: { get: () => null },
        });
        window.scrollTo = () => {};
        window.matchMedia = window.matchMedia || (() => ({
          matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
        }));
        // navigator.language ať setLang(...) dostane definovanou hodnotu
        try { Object.defineProperty(window.navigator, 'language', { value: 'en-US', configurable: true }); } catch (e) {}
      },
    });
  } catch (e) {
    errors.push('JSDOM load: ' + e.message);
  }
  return { errors };
}

let failed = 0, checked = 0;
console.log('\nSmartestGuide — JS smoke test\n');
for (const file of FILES) {
  const res = checkFile(file);
  if (res.skipped) { console.log(`  SKIP ${file} (nenalezen)`); continue; }
  checked++;
  if (res.errors.length) {
    failed++;
    console.log(`  FAIL ${file} — ${res.errors.length} JS chyb:`);
    res.errors.forEach((m) => console.log('       • ' + m));
  } else {
    console.log(`  OK   ${file} — skript proběhl bez chyb`);
  }
}
console.log(failed
  ? `\nVýsledek: ${checked - failed}/${checked} OK — ${failed} se selháním ❌`
  : `\nVýsledek: ${checked}/${checked} OK — bez JS chyb ✅`);
process.exit(failed ? 1 : 0);
