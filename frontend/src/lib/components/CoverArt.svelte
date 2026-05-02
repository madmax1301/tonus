<script lang="ts">
  import { extractHue, DEFAULT_HUE } from '$lib/accent';

  type Props = {
    src?: string | null;
    alt?: string;
    /** Pixel size; sets a square box. Ignored when `fluid` is true. */
    size?: number;
    /** Border-radius in px */
    radius?: number;
    /** Artist name — used for the gradient-fallback watermark initials. */
    artist?: string;
    /** Optional pre-known hue. If omitted, we extract from `src` when available. */
    hue?: number;
    /** Notify parent when hue is known (real or fallback). */
    onhue?: (hue: number) => void;
    class?: string;
    /**
     * When true, the cover fills its parent (width/height: 100%) — needed
     * when the parent uses `aspect-ratio: 1` to size the cover. Falls back
     * to a container-query for the watermark size so initials remain
     * proportional to the actual rendered width.
     */
    fluid?: boolean;
  };

  let {
    src,
    alt = '',
    size = 64,
    radius = 8,
    artist = '',
    hue: externalHue,
    onhue,
    class: extraClass = '',
    fluid = false
  }: Props = $props();

  let imgLoaded = $state(false);
  let imgErrored = $state(false);
  let derivedHue = $state<number | null>(externalHue ?? null);

  const h = $derived(derivedHue ?? externalHue ?? DEFAULT_HUE);
  const h2 = $derived((h + 40) % 360);
  const h3 = $derived((h + 200) % 360);

  const initials = $derived(
    artist
      ? artist
          .split(/\s+/)
          .map((w) => w[0])
          .join('')
          .slice(0, 2)
          .toUpperCase()
      : ''
  );

  $effect(() => {
    if (externalHue !== undefined) {
      derivedHue = externalHue;
      onhue?.(externalHue);
      return;
    }
    if (!src) {
      derivedHue = DEFAULT_HUE;
      onhue?.(DEFAULT_HUE);
      return;
    }
    let cancelled = false;
    extractHue(src).then((h) => {
      if (!cancelled) {
        derivedHue = h;
        onhue?.(h);
      }
    });
    return () => {
      cancelled = true;
    };
  });

  function onLoad() {
    imgLoaded = true;
  }
  function onError() {
    imgErrored = true;
  }
</script>

<div
  class="relative overflow-hidden flex-shrink-0 {extraClass}"
  style="
    {fluid
    ? 'width: 100%; height: 100%; container-type: inline-size;'
    : `width: ${size}px; height: ${size}px;`}
    border-radius: {radius}px;
    background:
      radial-gradient(120% 90% at 20% 10%, oklch(78% 0.15 {h}) 0%, transparent 55%),
      radial-gradient(110% 100% at 90% 95%, oklch(40% 0.12 {h3}) 0%, transparent 60%),
      linear-gradient(155deg, oklch(28% 0.06 {h2}) 0%, oklch(14% 0.04 {h}) 100%);
    box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.06);
  "
>
  {#if src && !imgErrored}
    <img
      {src}
      {alt}
      loading="lazy"
      decoding="async"
      crossorigin="anonymous"
      onload={onLoad}
      onerror={onError}
      class="absolute inset-0 w-full h-full object-cover transition-opacity duration-500"
      style="opacity: {imgLoaded ? 1 : 0};"
    />
  {/if}
  {#if (!imgLoaded || imgErrored || !src) && initials}
    <div
      class="absolute inset-0 flex items-center justify-center font-semibold"
      style="
        font-family: var(--font-display);
        color: rgba(255, 255, 255, 0.18);
        font-size: {fluid ? '42cqw' : `${Math.round(size * 0.42)}px`};
        letter-spacing: -0.02em;
        text-shadow: 0 2px 8px rgba(0, 0, 0, 0.4);
      "
    >
      {initials}
    </div>
  {/if}
</div>
