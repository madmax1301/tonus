<script lang="ts">
  type Size = 'xs' | 'sm' | 'md' | 'lg' | 'xl';

  type Props = {
    src?: string | null;
    alt?: string;
    size?: Size;
    class?: string;
  };

  let { src, alt = '', size = 'md', class: extraClass = '' }: Props = $props();

  const sizeMap: Record<Size, string> = {
    xs: 'w-9 h-9 rounded-md',
    sm: 'w-12 h-12 rounded-md',
    md: 'w-16 h-16 rounded-lg',
    lg: 'w-24 h-24 rounded-lg',
    xl: 'w-44 h-44 rounded-xl'
  };

  let loaded = $state(false);
  let errored = $state(false);

  function onLoad() {
    loaded = true;
  }
  function onError() {
    errored = true;
  }
</script>

<div
  class="{sizeMap[size]} {extraClass} relative overflow-hidden flex-shrink-0"
  style="background: var(--color-surface-3);"
>
  {#if src && !errored}
    <img
      {src}
      {alt}
      loading="lazy"
      decoding="async"
      onload={onLoad}
      onerror={onError}
      class="w-full h-full object-cover transition-opacity duration-300"
      style="opacity: {loaded ? 1 : 0};"
    />
  {/if}
  {#if !loaded || errored || !src}
    <div
      class="absolute inset-0 flex items-center justify-center text-[10px] font-medium tracking-widest"
      style="color: var(--color-fg-tertiary);"
    >
      ♪
    </div>
  {/if}
</div>
