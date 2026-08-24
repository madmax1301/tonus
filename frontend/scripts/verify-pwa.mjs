/**
 * Prüft das Build-Artefakt gegen die PWA-Akzeptanzkriterien.
 * Läuft nach `npm run build`. Exit 1 bei Verstoß.
 */
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { join } from 'node:path';

const BUILD = 'build';

function html() {
  return readFileSync(join(BUILD, 'index.html'), 'utf8');
}

function css() {
  const dir = join(BUILD, '_app', 'immutable', 'assets');
  // Hart fehlschlagen statt leer zurückgeben: ein leerer String würde
  // "enthält nicht"-Checks stillschweigend bestehen lassen.
  if (!existsSync(dir)) {
    throw new Error(`${dir} fehlt — wurde 'npm run build' ausgeführt?`);
  }
  const sheets = readdirSync(dir).filter((f) => f.endsWith('.css'));
  if (sheets.length === 0) {
    throw new Error(`keine CSS-Dateien in ${dir}`);
  }
  return sheets.map((f) => readFileSync(join(dir, f), 'utf8')).join('\n');
}

const CHECKS = [
  {
    name: 'keine externen Font-Hosts',
    run: () => {
      const haystack = html() + css();
      const hits = ['fonts.googleapis.com', 'fonts.gstatic.com'].filter((h) =>
        haystack.includes(h)
      );
      return hits.length === 0 || `gefunden: ${hits.join(', ')}`;
    }
  },
  {
    name: 'Web-Defaults abgeräumt',
    run: () => {
      // Whitespace normalisieren — der Build minifiziert die Deklarationen.
      // Auf exakte Werte prüfen, nicht nur auf den Property-Namen:
      // overscroll-behavior-x: contain (Nav-Scroller im Layout) darf hier
      // nicht als Treffer durchgehen.
      // -webkit-tap-highlight-color liefert bereits Tailwinds Preflight,
      // deshalb hier bewusst ungeprüft und im CSS nicht dupliziert.
      const sheet = css().replace(/\s+/g, '');
      const missing = ['-webkit-touch-callout:none', 'overscroll-behavior:none'].filter(
        (decl) => !sheet.includes(decl)
      );
      return missing.length === 0 || `fehlt: ${missing.join(', ')}`;
    }
  },
  {
    name: 'Safe-Area aktiviert',
    run: () => {
      const doc = html();
      const sheet = css();
      const problems = [];
      if (!doc.includes('viewport-fit=cover')) problems.push('viewport-fit=cover fehlt');
      if (!doc.includes('apple-mobile-web-app-status-bar-style'))
        problems.push('status-bar-style fehlt');
      if (!sheet.includes('safe-area-inset-top')) problems.push('safe-area-inset-top ungenutzt');
      return problems.length === 0 || problems.join(', ');
    }
  },
  {
    name: 'snapshot in Listen-Routen',
    run: () => {
      // Liest ausnahmsweise Quelldateien: snapshot ist eine Compile-Zeit-
      // Konvention von SvelteKit und taucht im Bundle nicht namentlich auf.
      const routes = ['src/routes/queue/+page.svelte', 'src/routes/import/+page.svelte'];
      const missing = routes.filter(
        (r) => !readFileSync(r, 'utf8').includes('export const snapshot')
      );
      return missing.length === 0 || `fehlt in: ${missing.join(', ')}`;
    }
  },
  {
    name: 'Mode-Strip scrollt statt das Dokument aufzuweiten',
    run: () => {
      // Fünf Tabs passen auf einem 390px-Viewport nicht nebeneinander. Ohne
      // eigenen Scroller wächst das Dokument horizontal und iOS skaliert die
      // ganze Seite herunter — die Bibliothek sieht dann "rausgezoomt" aus.
      const src = readFileSync('src/routes/+page.svelte', 'utf8');
      if (!src.includes('tonus-library-modes')) return 'Klasse .tonus-library-modes fehlt';
      const block = (src.split('.tonus-library-modes {')[1] || '').slice(0, 500);
      return block.includes('overflow-x: auto') || 'kein overflow-x: auto im Regelblock';
    }
  },
  {
    name: 'Lane-Strip begrenzt Grid-Items auf 0',
    run: () => {
      // `1fr` ist minmax(auto, 1fr) — die Mindestbreite ist min-content, also
      // sprengt eine Lane-Karte mit langem Track-Titel den Viewport und das
      // truncate darunter greift nie. Muss minmax(0, 1fr) bleiben.
      const src = readFileSync('src/routes/queue/+page.svelte', 'utf8');
      const block = (src.split('.tonus-lane-strip {')[1] || '').slice(0, 600);
      if (!block) return '.tonus-lane-strip-Regel nicht gefunden';
      return (
        block.includes('minmax(0, 1fr)') ||
        'grid-template-columns muss minmax(0, 1fr) sein, nicht 1fr'
      );
    }
  },
  {
    name: 'Service Worker gebaut, /api ausgenommen',
    run: () => {
      const path = join(BUILD, 'service-worker.js');
      if (!existsSync(path)) return 'build/service-worker.js fehlt';
      const sw = readFileSync(path, 'utf8');
      return sw.includes('/api/') || 'kein /api/-Ausschluss im Worker';
    }
  }
];

let failed = 0;
for (const check of CHECKS) {
  const result = check.run();
  if (result === true) {
    console.log(`  ok    ${check.name}`);
  } else {
    console.error(`  FAIL  ${check.name} — ${result}`);
    failed++;
  }
}

if (failed > 0) {
  console.error(`\n${failed} Check(s) fehlgeschlagen.`);
  process.exit(1);
}
console.log(`\n${CHECKS.length} Check(s) bestanden.`);
