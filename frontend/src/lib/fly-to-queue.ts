/**
 * Fly-to-Queue-Animation: zentraler Store + Trigger-Helper.
 *
 * Reproduziert die Spec aus design_handoff_tonus_b/SPEC_fly_to_queue.md:
 * Beim Klick auf einen "Queue"-Button fliegt der Cover-Klon des Tracks
 * auf einem 850 ms Bogen zur Vinyl-Puck unten rechts, schrumpft (scale
 * 0.36), rotiert um 360°, faded aus. Bei Ankunft: Queue-Counter +1,
 * Spule glüht 800 ms im Album-Akzent.
 *
 * Architektur:
 *  - flyingCovers: Live-Liste der gerade animierenden Klone — Layout
 *    mountet ein FlyingCover pro Eintrag.
 *  - queueCount + flashAccent: Reactive für den VinylPuck.
 *  - flyToQueue(): Trigger-Helper. Wird von Track-Cards / Album-Detail /
 *    Import-Recheck aufgerufen NACH erfolgreichem queueApi-Call.
 *
 * Koordinaten via offsetWithin-Traversal (transform-immun, im Gegensatz
 * zu getBoundingClientRect das bei CSS-Transforms eine Backdrop-Hell
 * verursacht).
 */
import { writable, get } from 'svelte/store';

export type FlyingCover = {
  id: number;
  src: string | null;
  accent: string;
  size: number;
  from: { x: number; y: number };
  to: { x: number; y: number };
};

/** Live-Animationen — Layout iteriert darüber und mounted FlyingCover-Komponenten. */
export const flyingCovers = writable<FlyingCover[]>([]);

/** Counter im Vinyl-Puck. Wird bei flush +1 gebumpt; Initial-Wert kommt
 *  vom ersten Queue-Status-Read im Layout. */
export const queueCount = writable<number>(0);

/** Akzentfarbe für den 800ms-Flash der Spule bei Ankunft. */
export const flashAccent = writable<string | null>(null);

let flyIdCounter = 0;

/**
 * Lös die Cover-fly-Animation aus.
 *
 * @param coverEl  Das DOM-Element des Original-Covers (Track-Row).
 *                 Wird via offsetWithin gegen document.body gemessen.
 *                 Während des Fluges versteckt der Caller das Original
 *                 manuell (visibility: hidden) damit kein Reflow passiert.
 * @param src      URL des Cover-Bilds — der Klon zeigt dasselbe Bild.
 * @param accent   Akzent-Farbe für Flug-Box-Shadow + Puck-Flash.
 * @param size     Cover-Pixel-Größe (default 48 für Track-Rows).
 *
 * @returns Promise das nach 850 ms (Flug-Ende) resolved. Caller kann
 *          danach z.B. das Original-Cover wieder einblenden.
 */
export function flyToQueue(
  coverEl: HTMLElement,
  src: string | null,
  accent: string,
  size = 48
): Promise<void> {
  if (typeof document === 'undefined') return Promise.resolve();
  const puck = document.querySelector<HTMLElement>('[data-vinyl-puck]');
  if (!puck) {
    // Puck noch nicht gemounted (z.B. Fast-Click vor Layout-Hydration) —
    // Animation skippen, aber Counter trotzdem bumpen.
    queueCount.update((n) => n + 1);
    return Promise.resolve();
  }

  // Viewport-relative Koordinaten via getBoundingClientRect() — der Puck
  // ist `position: fixed` (klebt an viewport.right/bottom), und der Klon
  // wird ebenfalls als position: fixed gerendert. Beide also im selben
  // Koordinatensystem (window). Vorteil gegenüber dem alten offsetWithin:
  //   - scroll-immun (Source-Cover scrollt mit, Puck nicht — Klon nutzt
  //     viewport-Koordinaten und rechnet nicht mehr falsch wenn der User
  //     gescrollt hat)
  //   - kein Margin-Geraffel mit fixed-Elementen, deren offsetTop bei
  //     `bottom: 24` undefiniert oder 0 ist
  const coverRect = coverEl.getBoundingClientRect();
  const puckRect = puck.getBoundingClientRect();

  // Cover-Klon zentriert auf Cover-Mittelpunkt (Source) bzw. Puck-Mittel
  // (Target). FlyingCover positioniert via translate(x, y) ohne weitere
  // Anchor-Korrektur, also rechnen wir hier x = center - size/2.
  const fromX = coverRect.left + coverRect.width / 2 - size / 2;
  const fromY = coverRect.top + coverRect.height / 2 - size / 2;
  // Bei Ankunft soll der Klon auf 0.36 geschrumpft sein. Ziel-Center =
  // Puck-Center. transform: translate(...) bezieht sich aufs unscaled
  // Element — also verschiebe so, dass der ungeschrumpfte Klon mittig
  // unter dem Puck-Center liegt; die scale(0.36) zieht ihn dann optisch
  // zur Mitte zusammen. Visuell macht das den Bogen schöner.
  const toX = puckRect.left + puckRect.width / 2 - size / 2;
  const toY = puckRect.top + puckRect.height / 2 - size / 2;

  const id = ++flyIdCounter;
  flyingCovers.update((arr) => [
    ...arr,
    {
      id,
      src,
      accent,
      size,
      from: { x: fromX, y: fromY },
      to: { x: toX, y: toY }
    }
  ]);

  return new Promise<void>((resolve) => {
    window.setTimeout(() => {
      // Counter bumpen + Puck flashen
      queueCount.update((n) => n + 1);
      flashAccent.set(accent);
      window.setTimeout(() => flashAccent.set(null), 800);
      // Klon entfernen
      flyingCovers.update((arr) => arr.filter((f) => f.id !== id));
      resolve();
    }, 850);
  });
}

/** Sync the queue counter with the actual backend value. Layout polls
 *  the queue endpoint and keeps the puck in sync — wenn fly-to-queue
 *  optimistisch +1 macht und das Backend zurück mit z.B. 7 antwortet,
 *  zeigt der Puck korrekt 7 (statt einer Drift). */
export function setQueueCount(n: number): void {
  queueCount.set(n);
}

export function getQueueCount(): number {
  return get(queueCount);
}
