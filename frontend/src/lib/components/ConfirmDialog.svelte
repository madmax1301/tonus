<script lang="ts">
  /**
   * Cinematic Confirm-Dialog. Im Layout einmalig gemountet, lauscht
   * auf den dialogState-Store und rendert wenn ein Aufruf da ist.
   *
   * Backdrop: rgba(0,0,0,0.55) + blur(20px), schließt bei Click
   * (außerhalb der Card).
   * Card: Glass-Panel mit Eyebrow + Display-Title + Body + 2 Buttons.
   * Keys: Esc → cancel, Enter → confirm (autofocus auf Confirm-Btn).
   */
  import { onMount } from 'svelte';
  import { dialogState } from '$lib/confirm';
  import { tint, DEFAULT_HUE } from '$lib/accent';

  const accent = tint(DEFAULT_HUE);

  let confirmBtn: HTMLButtonElement | null = $state(null);

  function close(ok: boolean) {
    const state = $dialogState;
    if (!state) return;
    state.resolve(ok);
    dialogState.set(null);
  }

  function onKeydown(e: KeyboardEvent) {
    if (!$dialogState) return;
    if (e.key === 'Escape') {
      e.preventDefault();
      close(false);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      close(true);
    }
  }

  onMount(() => {
    window.addEventListener('keydown', onKeydown);
    return () => window.removeEventListener('keydown', onKeydown);
  });

  // Autofocus auf Confirm-Button beim Öffnen — direkt Enter drücken
  // bestätigt, einmal Tab landet auf Cancel.
  $effect(() => {
    if ($dialogState && confirmBtn) {
      requestAnimationFrame(() => confirmBtn?.focus());
    }
  });

  const destructiveColor = '#ff453a';
</script>

{#if $dialogState}
  {@const opt = $dialogState.options}
  {@const btnColor = opt.destructive ? destructiveColor : accent}
  <!-- Backdrop -->
  <div
    role="presentation"
    onclick={() => close(false)}
    class="tonus-confirm-backdrop"
    style="
      position: fixed;
      inset: 0;
      z-index: 100;
      background: rgba(0, 0, 0, 0.55);
      backdrop-filter: blur(20px) saturate(0.8);
      -webkit-backdrop-filter: blur(20px) saturate(0.8);
      display: flex;
      align-items: center;
      justify-content: center;
      animation: tonus-fade-in 0.2s ease-out;
    "
  >
    <!-- Card — stoppt click-propagation damit Backdrop-Click nicht greift -->
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="tonus-confirm-title"
      onclick={(e) => e.stopPropagation()}
      onkeydown={(e) => e.stopPropagation()}
      tabindex="-1"
      class="tonus-confirm-card"
      style="
        position: relative;
        max-width: 480px;
        width: calc(100% - 48px);
        padding: 30px 32px 26px;
        border-radius: 22px;
        background: rgba(20, 20, 24, 0.85);
        backdrop-filter: blur(40px) saturate(1.2);
        -webkit-backdrop-filter: blur(40px) saturate(1.2);
        border: 1px solid var(--color-border-soft);
        box-shadow:
          0 32px 80px rgba(0, 0, 0, 0.6),
          0 0 0 1px rgba(255, 255, 255, 0.04),
          inset 0 1px 0 rgba(255, 255, 255, 0.06){opt.destructive
          ? `, 0 0 60px ${destructiveColor}22`
          : ''};
        animation: tonus-confirm-rise 0.3s cubic-bezier(0.16, 1, 0.3, 1);
      "
    >
      <div
        class="font-semibold uppercase"
        style="
          font-size: 11px;
          letter-spacing: 0.24em;
          color: {btnColor};
          margin-bottom: 10px;
        "
      >
        Bestätigen
      </div>

      <h2
        id="tonus-confirm-title"
        class="m-0"
        style="
          font-family: var(--font-display);
          font-size: 22px;
          font-weight: 500;
          letter-spacing: -0.02em;
          color: var(--color-fg-primary);
          line-height: 1.2;
        "
      >
        {opt.title}
      </h2>

      {#if opt.message}
        <p
          style="
            font-size: 13.5px;
            color: var(--color-fg-secondary);
            line-height: 1.55;
            margin: 12px 0 0;
            max-width: 420px;
          "
        >
          {opt.message}
        </p>
      {/if}

      <div class="flex items-center justify-end gap-2 mt-7">
        <button
          type="button"
          onclick={() => close(false)}
          class="inline-flex items-center transition-colors"
          style="
            padding: 9px 18px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 500;
            letter-spacing: 0.02em;
            background: rgba(255, 255, 255, 0.04);
            color: var(--color-fg-secondary);
            border: 1px solid var(--color-border-soft);
          "
        >
          {opt.cancelLabel ?? 'Abbrechen'}
        </button>
        <button
          type="button"
          bind:this={confirmBtn}
          onclick={() => close(true)}
          class="inline-flex items-center transition-transform"
          style="
            padding: 9px 22px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 600;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            background: {btnColor};
            color: #0a0a0c;
            border: none;
            box-shadow: 0 8px 24px {btnColor}40;
          "
        >
          {opt.confirmLabel ?? 'OK'}
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  @keyframes tonus-fade-in {
    from {
      opacity: 0;
    }
    to {
      opacity: 1;
    }
  }
  @keyframes tonus-confirm-rise {
    from {
      opacity: 0;
      transform: translateY(12px) scale(0.98);
    }
    to {
      opacity: 1;
      transform: translateY(0) scale(1);
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .tonus-confirm-backdrop,
    .tonus-confirm-card {
      animation: none;
    }
  }
  .tonus-confirm-card:focus {
    outline: none;
  }
</style>
