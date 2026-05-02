<script lang="ts">
  import CoverArt from './CoverArt.svelte';
  import { tint, DEFAULT_HUE } from '$lib/accent';

  type Props = {
    src?: string | null;
    alt?: string;
    artist?: string;
    /** Year shown on disc label */
    year?: number | string | null;
    hue?: number;
    /** Pixel size of the cover; disc takes 55% additional width to the right. */
    size?: number;
    /** Spin animation when album is "active" — respects prefers-reduced-motion. */
    spinning?: boolean;
    onhue?: (hue: number) => void;
  };

  let {
    src,
    alt = '',
    artist = '',
    year = null,
    hue,
    size = 200,
    spinning = false,
    onhue
  }: Props = $props();

  let derivedHue = $state<number>(hue ?? DEFAULT_HUE);

  const h = $derived(hue ?? derivedHue);
  const offset = $derived(size * 0.55);
  const accent = $derived(tint(h));
  const labelStop1 = $derived(`oklch(40% 0.10 ${h})`);
  const labelStop2 = $derived(`oklch(22% 0.05 ${h})`);

  function handleHue(next: number) {
    derivedHue = next;
    onhue?.(next);
  }

  const firstName = $derived(artist.split(/\s+/)[0] ?? '');
</script>

<div
  class="relative flex-shrink-0"
  style="width: {size + offset}px; height: {size}px;"
>
  <!-- Vinyl disc, peeks out behind the cover -->
  <div
    class="tonus-vinyl-disc absolute top-0"
    style="
      right: 0;
      width: {size}px;
      height: {size}px;
      border-radius: 50%;
      background:
        radial-gradient(circle at 50% 50%, {accent} 0 {size * 0.08}px, #18120c {size * 0.08}px {size * 0.1}px, transparent {size * 0.1}px),
        repeating-radial-gradient(circle at 50% 50%, #0a0a0c 0 1px, #161618 1px 3px),
        radial-gradient(circle at 35% 30%, rgba(255, 255, 255, 0.08), transparent 50%),
        #0a0a0c;
      box-shadow: 0 12px 32px rgba(0, 0, 0, 0.5), 0 0 0 1px rgba(255, 255, 255, 0.04);
      animation: {spinning ? 'tonus-spin 12s linear infinite' : 'none'};
    "
  >
    <!-- Center label -->
    <div
      class="absolute left-1/2 top-1/2 flex flex-col items-center justify-center text-center"
      style="
        transform: translate(-50%, -50%);
        width: {size * 0.34}px;
        height: {size * 0.34}px;
        border-radius: 50%;
        padding: 6px;
        background: radial-gradient(circle, {labelStop1}, {labelStop2});
        box-shadow: inset 0 0 0 1px rgba(0, 0, 0, 0.4);
      "
    >
      {#if firstName}
        <div
          class="font-semibold uppercase"
          style="
            font-size: {size * 0.04}px;
            letter-spacing: 0.18em;
            color: rgba(255, 255, 255, 0.65);
            font-family: var(--font-display);
          "
        >
          {firstName}
        </div>
      {/if}
      <div
        class="my-1"
        style="width: 40%; height: 1px; background: rgba(255, 255, 255, 0.2);"
      ></div>
      {#if year}
        <div
          style="
            font-size: {size * 0.034}px;
            color: rgba(255, 255, 255, 0.45);
            font-family: var(--font-mono);
          "
        >
          {year}
        </div>
      {/if}
    </div>
  </div>

  <!-- Cover, front-left -->
  <div
    class="absolute left-0 top-0 overflow-hidden"
    style="
      width: {size}px;
      height: {size}px;
      border-radius: 4px;
      box-shadow: 0 16px 40px rgba(0, 0, 0, 0.55), 0 0 0 1px rgba(255, 255, 255, 0.05);
    "
  >
    <CoverArt {src} {alt} {artist} hue={hue} size={size} radius={4} onhue={handleHue} />
  </div>
</div>
