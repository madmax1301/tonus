<script lang="ts">
  import { tint, tintDeep, DEFAULT_HUE } from '$lib/accent';

  type Props = {
    hue?: number | null;
    intensity?: number;
  };

  let { hue = DEFAULT_HUE, intensity = 1 }: Props = $props();

  const h = $derived(hue ?? DEFAULT_HUE);
</script>

<!--
  Fixed positioning + GPU compositing layer ensures the expensive blur+saturate
  filter rasterizes once and is never re-evaluated during scroll. Without
  `position: fixed` and `transform: translateZ(0)`, the browser must reblur
  the entire viewport on every scroll frame.
-->
<div
  aria-hidden="true"
  class="pointer-events-none fixed inset-0 z-0"
  style="
    background:
      radial-gradient(60% 50% at 18% 25%, {tint(h, 0.55 * intensity)} 0%, transparent 60%),
      radial-gradient(50% 50% at 82% 80%, {tintDeep(h, 0.6 * intensity)} 0%, transparent 65%),
      var(--color-surface-0);
    filter: blur(60px) saturate(1.2);
    transform: translateZ(0);
    will-change: transform;
    contain: strict;
  "
></div>
