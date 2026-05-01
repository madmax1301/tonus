<script lang="ts">
  type Props = {
    /** 0..100. Wenn undefined und pareto=true: animiert bis 95% nach Pareto-Pacing. */
    value?: number;
    /**
     * Pareto-Pacing während wir auf den Server warten:
     *   0 → 80 %    in   4 s   (cubic-out, "viel passiert schnell")
     *   80 → 95 %   in  26 s   (slow-quart, "letzte Meter dauern")
     * Sobald `value` gesetzt ist, übernimmt der echte Wert.
     */
    pareto?: boolean;
    /** done=true → Bar springt auf 100 % (kein shimmer mehr). */
    done?: boolean;
    /** Sehr dünn (1.5px) statt Default 2px — für inline-button Use. */
    thin?: boolean;
  };

  let { value, pareto = false, done = false, thin = false }: Props = $props();

  const hasReal = $derived(typeof value === 'number' && value >= 0);
  const clamped = $derived(hasReal ? Math.max(0, Math.min(100, value as number)) : 0);
  const showPareto = $derived(pareto && !done && !hasReal);
</script>

<div
  class="track"
  class:thin
  style="background: var(--color-border-soft);"
>
  {#if done}
    <div class="fill" style="width: 100%; background: var(--color-status-done);"></div>
  {:else if hasReal}
    <div class="fill" style="width: {clamped}%; background: var(--color-accent);"></div>
  {:else if showPareto}
    <div class="fill pareto" style="background: var(--color-accent);">
      <div class="shimmer"></div>
    </div>
  {:else}
    <div class="fill indeterminate" style="background: var(--color-accent);"></div>
  {/if}
</div>

<style>
  .track {
    position: relative;
    width: 100%;
    height: 2px;
    overflow: hidden;
    border-radius: 999px;
  }
  .track.thin {
    height: 1.5px;
  }
  .fill {
    position: absolute;
    inset: 0 auto 0 0;
    height: 100%;
    border-radius: inherit;
    transition: width 0.5s cubic-bezier(0.16, 1, 0.3, 1);
  }
  .fill.pareto {
    width: 0;
    animation:
      pareto-fast 4s cubic-bezier(0.16, 1, 0.3, 1) forwards,
      pareto-slow 26s cubic-bezier(0.4, 0, 0.7, 0.2) 4s forwards;
  }
  @keyframes pareto-fast {
    to {
      width: 80%;
    }
  }
  @keyframes pareto-slow {
    to {
      width: 95%;
    }
  }
  .fill.pareto .shimmer {
    position: absolute;
    inset: 0;
    background: linear-gradient(
      90deg,
      transparent 0%,
      rgba(255, 255, 255, 0.35) 50%,
      transparent 100%
    );
    transform: translateX(-100%);
    animation: bar-shimmer 1.4s linear infinite;
  }
  @keyframes bar-shimmer {
    to {
      transform: translateX(100%);
    }
  }
  .fill.indeterminate {
    width: 30%;
    animation: indeterminate 1.6s ease-in-out infinite;
  }
  @keyframes indeterminate {
    0% {
      left: -30%;
    }
    100% {
      left: 100%;
    }
  }
</style>
