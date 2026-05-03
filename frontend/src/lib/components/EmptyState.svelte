<script lang="ts">
  /**
   * Generischer cinematic Empty-State analog zu SPEC_empty_states.md.
   * Eyebrow + Display-Title + Body + Action-Row + optional Tipp-Footer.
   *
   * Glyph-Slots:
   *   - "library": dashed crate mit drei gestaffelten Album-Silhouetten
   *   - "queue": pulsierender Vinyl-Puck (140 px, ohne Counter)
   *
   * Usage:
   *   <EmptyState
   *     glyph="library"
   *     eyebrow="Kein Album bisher"
   *     title="Deine Bibliothek\nwartet auf den ersten Track."
   *     body="Suche einen Song oder importiere eine CSV ..."
   *     tip="Tipp: ↵ in der Suchleiste lädt das erste Treffer-Match."
   *   >
   *     {#snippet actions()}
   *       <button>...</button>
   *     {/snippet}
   *   </EmptyState>
   */
  import type { Snippet } from 'svelte';
  import { tint, DEFAULT_HUE } from '$lib/accent';

  type Glyph = 'library' | 'queue' | 'none';

  interface Props {
    glyph?: Glyph;
    eyebrow: string;
    title: string;
    body?: string;
    tip?: string;
    actions?: Snippet;
  }

  let { glyph = 'none', eyebrow, title, body, tip, actions }: Props = $props();

  const accent = tint(DEFAULT_HUE);
</script>

<section
  class="tonus-empty-state"
  style="
    min-height: 680px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    text-align: center;
    padding: 60px 36px 80px;
    gap: 24px;
  "
>
  {#if glyph === 'library'}
    <!-- Empty-Crate Glyph: dashed-Kiste mit 3 gestaffelten Album-Silhouetten -->
    <div
      class="tonus-empty-glyph-crate"
      style="
        position: relative;
        width: 180px;
        height: 180px;
        border: 1.5px dashed rgba(255, 255, 255, 0.18);
        border-radius: 20px;
        background: rgba(255, 255, 255, 0.02);
      "
    >
      {#each [0, 1, 2] as i}
        <div
          style="
            position: absolute;
            top: {30 + i * 4}px;
            left: {30 + i * 8}px;
            width: 60px;
            height: 120px;
            background: linear-gradient(180deg, rgba(255, 255, 255, 0.08), rgba(255, 255, 255, 0.02));
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 3px;
            transform: rotate({(i - 1) * 4}deg);
            opacity: {0.4 - i * 0.1};
          "
        ></div>
      {/each}
    </div>
  {:else if glyph === 'queue'}
    <!-- Vinyl-Puck-Glyph: 140 px, ohne Counter, pulsiert sanft -->
    <div
      class="tonus-empty-glyph-vinyl"
      style="
        width: 140px;
        height: 140px;
        border-radius: 50%;
        background:
          radial-gradient(circle at 50% 50%, {accent} 0 8px, #18120c 8px 11px, transparent 11px),
          repeating-radial-gradient(circle at 50% 50%, #0a0a0c 0 1.5px, #161618 1.5px 4.5px),
          #0a0a0c;
        box-shadow:
          0 16px 48px rgba(0, 0, 0, 0.55),
          0 0 0 1px rgba(255, 255, 255, 0.06);
        position: relative;
      "
    >
      <span
        style="
          position: absolute;
          inset: 30%;
          border-radius: 50%;
          background: radial-gradient(circle, {tint(DEFAULT_HUE, 0.7)}, oklch(22% 0.05 30));
        "
      ></span>
    </div>
  {/if}

  <div
    class="font-semibold uppercase"
    style="
      font-size: 11px;
      letter-spacing: 0.24em;
      color: {accent};
      font-weight: 600;
    "
  >
    {eyebrow}
  </div>

  <h2
    class="m-0"
    style="
      font-family: var(--font-display);
      font-size: 48px;
      font-weight: 600;
      letter-spacing: -0.035em;
      line-height: 1;
      color: var(--color-fg-primary);
      max-width: 600px;
      white-space: pre-line;
    "
  >
    {title}
  </h2>

  {#if body}
    <p
      style="
        font-size: 15px;
        font-weight: 300;
        line-height: 1.55;
        color: var(--color-fg-secondary);
        max-width: 520px;
        margin: 0;
      "
    >
      {body}
    </p>
  {/if}

  {#if actions}
    <div class="flex items-center gap-2 flex-wrap justify-center" style="margin-top: 8px;">
      {@render actions()}
    </div>
  {/if}

  {#if tip}
    <div
      style="
        margin-top: 24px;
        padding: 14px 18px;
        background: rgba(255, 255, 255, 0.03);
        border: 1px solid rgba(255, 255, 255, 0.1);
        border-radius: 12px;
        font-family: var(--font-mono);
        font-size: 11.5px;
        letter-spacing: 0.05em;
        color: var(--color-fg-tertiary);
        max-width: 600px;
      "
    >
      💡 {tip}
    </div>
  {/if}
</section>

<style>
  .tonus-empty-glyph-vinyl {
    animation: tonus-empty-pulse 2.4s ease-in-out infinite alternate;
  }
  @keyframes tonus-empty-pulse {
    from {
      opacity: 0.6;
      transform: scale(1);
    }
    to {
      opacity: 1;
      transform: scale(1.03);
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .tonus-empty-glyph-vinyl {
      animation: none;
      opacity: 0.85;
    }
  }
</style>
