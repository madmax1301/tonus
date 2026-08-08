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
