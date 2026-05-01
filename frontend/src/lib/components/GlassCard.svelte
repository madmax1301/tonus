<script lang="ts">
  type Tone = 'card' | 'modal';
  type Padding = 'none' | 'sm' | 'md' | 'lg';

  type Props = {
    tone?: Tone;
    padding?: Padding;
    interactive?: boolean;
    class?: string;
    children?: import('svelte').Snippet;
  };

  let {
    tone = 'card',
    padding = 'md',
    interactive = false,
    class: extraClass = '',
    children
  }: Props = $props();

  const padMap: Record<Padding, string> = {
    none: '',
    sm: 'p-3',
    md: 'p-5',
    lg: 'p-7'
  };

  const surface = $derived(tone === 'modal' ? 'var(--color-surface-2)' : 'var(--color-surface-1)');
  const blur = $derived(tone === 'modal' ? 'var(--blur-modal)' : 'var(--blur-card)');
</script>

<div
  class="rounded-[var(--radius-lg)] {padMap[padding]} {extraClass}"
  class:transition-colors={interactive}
  class:cursor-pointer={interactive}
  style="background: {surface}; border: 1px solid var(--color-border-soft); backdrop-filter: blur({blur});"
>
  {@render children?.()}
</div>

<style>
  div {
    -webkit-backdrop-filter: var(--apple-fallback);
  }
  div:hover {
    border-color: var(--color-border-firm);
  }
</style>
