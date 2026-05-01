<script lang="ts">
  type Props = {
    /** 0..100 oder undefined = indeterminate */
    value?: number;
    indeterminate?: boolean;
  };

  let { value, indeterminate = false }: Props = $props();

  const clamped = $derived(typeof value === 'number' ? Math.max(0, Math.min(100, value)) : 0);
  const showIndeterminate = $derived(indeterminate || typeof value !== 'number');
</script>

<div
  class="relative w-full h-[2px] overflow-hidden rounded-full"
  style="background: var(--color-border-soft);"
>
  {#if showIndeterminate}
    <div class="absolute inset-y-0 sweep" style="background: var(--color-accent);"></div>
  {:else}
    <div
      class="absolute inset-y-0 left-0 transition-[width] duration-500 ease-out"
      style="width: {clamped}%; background: var(--color-accent);"
    ></div>
  {/if}
</div>

<style>
  .sweep {
    width: 30%;
    animation: sweep 1.6s ease-in-out infinite;
  }
  @keyframes sweep {
    0% {
      left: -30%;
    }
    100% {
      left: 100%;
    }
  }
</style>
