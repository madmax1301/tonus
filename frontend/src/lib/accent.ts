/**
 * Per-album dynamic accent extraction.
 *
 * Production replacement for the Direction-B mocks' hand-coded `hue` field:
 * we sample 16×16 pixels from the actual cover art, pick the most-saturated
 * average, and convert RGB → HSL hue. The `oklch(70% 0.18 H)` recipe stays
 * untouched — only the hue source changes.
 *
 * Caching: per cover-URL, in-memory for the session. The Map persists as
 * long as the module is alive, so repeat visits to the same album reuse
 * the work.
 */

export const DEFAULT_HUE = 38; // warm gold #c8a96a as fallback

export type AccentColor = {
  hue: number;
  /** `oklch(70% 0.18 H / a)` — bright accent for buttons, rings, progress */
  tint: (alpha?: number) => string;
  /** `oklch(40% 0.14 H / a)` — saturated background tone for backdrops */
  tintDeep: (alpha?: number) => string;
};

const cache = new Map<string, number>();
const inflight = new Map<string, Promise<number>>();

export function tint(hue: number, alpha = 1): string {
  return `oklch(70% 0.18 ${hue} / ${alpha})`;
}

export function tintDeep(hue: number, alpha = 1): string {
  return `oklch(40% 0.14 ${hue} / ${alpha})`;
}

export function accentFor(hue: number): AccentColor {
  return {
    hue,
    tint: (a = 1) => tint(hue, a),
    tintDeep: (a = 1) => tintDeep(hue, a)
  };
}

/**
 * Best-effort dominant-hue extraction. Returns `DEFAULT_HUE` on any failure
 * (no URL, CORS-block, decode error, all-grey image).
 */
export async function extractHue(url: string | null | undefined): Promise<number> {
  if (!url) return DEFAULT_HUE;
  if (cache.has(url)) return cache.get(url)!;
  if (inflight.has(url)) return inflight.get(url)!;

  const promise = doExtract(url).then((h) => {
    cache.set(url, h);
    inflight.delete(url);
    return h;
  });
  inflight.set(url, promise);
  return promise;
}

/** Run a callback when the browser is idle. Falls back to a microtask
 *  (with a tiny setTimeout buffer) on Safari, where requestIdleCallback
 *  isn't natively available.
 */
function whenIdle(): Promise<void> {
  return new Promise((resolve) => {
    if (typeof window === 'undefined') {
      resolve();
      return;
    }
    const ric = (window as unknown as { requestIdleCallback?: (cb: () => void, opts?: { timeout: number }) => number })
      .requestIdleCallback;
    if (ric) {
      ric(() => resolve(), { timeout: 1500 });
    } else {
      setTimeout(resolve, 50);
    }
  });
}

async function doExtract(url: string): Promise<number> {
  if (typeof window === 'undefined') return DEFAULT_HUE;
  try {
    const img = new Image();
    img.crossOrigin = 'anonymous';
    img.decoding = 'async';
    await new Promise<void>((resolve, reject) => {
      img.onload = () => resolve();
      img.onerror = () => reject(new Error('image load failed'));
      img.src = url;
    });

    // Wait for an idle slot before doing the canvas work — the drawImage +
    // getImageData + per-pixel loop is ~1-3 ms on a small image but compounds
    // when 20 cards extract in parallel during a scroll. requestIdleCallback
    // means we yield to the user's scroll thread first.
    await whenIdle();

    const size = 16;
    const canvas =
      typeof OffscreenCanvas !== 'undefined'
        ? new OffscreenCanvas(size, size)
        : Object.assign(document.createElement('canvas'), { width: size, height: size });
    const ctx = (canvas as unknown as { getContext: (t: string) => CanvasRenderingContext2D | null }).getContext(
      '2d'
    );
    if (!ctx) return DEFAULT_HUE;
    ctx.drawImage(img as CanvasImageSource, 0, 0, size, size);
    const { data } = ctx.getImageData(0, 0, size, size);

    // Pick the pixel with the highest saturation × value, weighted by frequency.
    // Bucket hues into 12 bins of 30° each, accumulate weighted score.
    const buckets = new Float32Array(12);
    const bucketHue = new Float32Array(12);
    for (let i = 0; i < data.length; i += 4) {
      const r = data[i] / 255;
      const g = data[i + 1] / 255;
      const b = data[i + 2] / 255;
      const max = Math.max(r, g, b);
      const min = Math.min(r, g, b);
      const v = max;
      const s = max === 0 ? 0 : (max - min) / max;
      if (s < 0.18 || v < 0.15) continue; // skip near-greys and deep shadows
      const h = rgbToHue(r, g, b, max, min);
      const idx = Math.floor(h / 30) % 12;
      const score = s * v;
      buckets[idx] += score;
      bucketHue[idx] += h * score;
    }

    let bestIdx = -1;
    let bestScore = 0;
    for (let i = 0; i < 12; i++) {
      if (buckets[i] > bestScore) {
        bestScore = buckets[i];
        bestIdx = i;
      }
    }
    if (bestIdx === -1) return DEFAULT_HUE;
    return bucketHue[bestIdx] / buckets[bestIdx];
  } catch {
    return DEFAULT_HUE;
  }
}

function rgbToHue(r: number, g: number, b: number, max: number, min: number): number {
  if (max === min) return 0;
  const d = max - min;
  let h: number;
  if (max === r) h = ((g - b) / d) % 6;
  else if (max === g) h = (b - r) / d + 2;
  else h = (r - g) / d + 4;
  h *= 60;
  if (h < 0) h += 360;
  return h;
}
