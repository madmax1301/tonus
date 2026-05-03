<script lang="ts">
  import { onMount } from 'svelte';

  interface Props {
    src: string | null;
    accent: string;
    size: number;
    from: { x: number; y: number };
    to: { x: number; y: number };
  }
  let { src, accent, size, from, to }: Props = $props();

  // Double-RAF-Trick: erster Mount-Zustand commited mit `from`-Position
  // (translate-from). Nach 2× requestAnimationFrame setzen wir mounted
  // = true → Browser interpoliert via CSS-Transition zur `to`-Position.
  // Ohne den Doppel-RAF springt der Stil oft direkt zur End-Position
  // (Browser collapsed Initial- und Final-Style in einem Layout-Pass).
  let mounted = $state(false);
  onMount(() => {
    requestAnimationFrame(() => requestAnimationFrame(() => (mounted = true)));
  });

  // Reduced-motion: keine Transition → User sieht direkt den End-Zustand
  // (counter bumpt trotzdem über setTimeout im Store). a11y-konform.
  const prefersReducedMotion = $derived(
    typeof window !== 'undefined'
      ? window.matchMedia('(prefers-reduced-motion: reduce)').matches
      : false
  );
</script>

<div
  class="tonus-fly-cover"
  style="
    position: absolute;
    z-index: 60;
    pointer-events: none;
    left: 0;
    top: 0;
    width: {size}px;
    height: {size}px;
    transform: {mounted
      ? `translate(${to.x}px, ${to.y}px) scale(0.36) rotate(360deg)`
      : `translate(${from.x}px, ${from.y}px) scale(1) rotate(0deg)`};
    opacity: {mounted ? 0 : 1};
    transition: {prefersReducedMotion
      ? 'none'
      : 'transform 0.85s cubic-bezier(0.4, 0, 0.2, 1), opacity 0.85s ease-in'};
  "
>
  <div
    style="
      width: {size}px;
      height: {size}px;
      border-radius: 6px;
      overflow: hidden;
      box-shadow: 0 12px 28px {accent}80, 0 0 0 2px {accent}55;
    "
  >
    {#if src}
      <img
        {src}
        alt=""
        loading="eager"
        decoding="sync"
        style="width: 100%; height: 100%; object-fit: cover; display: block;"
      />
    {:else}
      <div
        style="
          width: 100%;
          height: 100%;
          background: linear-gradient(135deg, {accent}, oklch(35% 0.15 30));
        "
      ></div>
    {/if}
  </div>
</div>
