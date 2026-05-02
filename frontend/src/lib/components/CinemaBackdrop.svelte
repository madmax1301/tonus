<script lang="ts">
  import { tint, tintDeep, DEFAULT_HUE } from '$lib/accent';

  type Props = {
    hue?: number | null;
    intensity?: number;
  };

  let { hue = DEFAULT_HUE, intensity = 1 }: Props = $props();

  const h = $derived(hue ?? DEFAULT_HUE);
</script>

<div
  aria-hidden="true"
  class="pointer-events-none absolute inset-[-15%] z-0"
  style="
    background:
      radial-gradient(60% 50% at 18% 25%, {tint(h, 0.55 * intensity)} 0%, transparent 60%),
      radial-gradient(50% 50% at 82% 80%, {tintDeep(h, 0.6 * intensity)} 0%, transparent 65%),
      var(--color-surface-0);
    filter: blur(60px) saturate(1.2);
  "
></div>
