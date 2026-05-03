<script lang="ts">
  /**
   * Cinematic Token-Sheet — Glass-Center-Modal statt Bottom-Sheet.
   * Stilkonsistent mit ConfirmDialog: Backdrop blur(20px), Glass-Card mit
   * Editorial-Eyebrow ("BEARER · backend/.env"), Display-Title, accent-
   * gold Save-Button mit Glow.
   *
   * Verwendet bits-ui Dialog für Portal/A11y, aber das visuelle Styling
   * matcht ConfirmDialog/Settings/Library pixelgenau.
   */
  import { Dialog } from 'bits-ui';
  import { apiToken, authChallengeOpen, dismissChallenge } from '$lib/auth';
  import { tint, DEFAULT_HUE } from '$lib/accent';
  import { Check, X, KeyRound } from 'lucide-svelte';

  const accent = tint(DEFAULT_HUE);

  let value = $state('');
  let saved = $state(false);

  $effect(() => {
    if ($authChallengeOpen) {
      // Vor-befüllen mit aktuellem Token, wenn vorhanden
      value = $apiToken;
      saved = false;
    }
  });

  function save() {
    apiToken.set(value.trim());
    saved = true;
    setTimeout(() => {
      dismissChallenge();
      saved = false;
    }, 600);
  }

  function close() {
    dismissChallenge();
  }
</script>

<Dialog.Root bind:open={$authChallengeOpen}>
  <Dialog.Portal>
    <Dialog.Overlay
      class="fixed inset-0 z-[90] tonus-token-overlay"
      style="
        background: rgba(0, 0, 0, 0.55);
        backdrop-filter: blur(20px) saturate(0.8);
        -webkit-backdrop-filter: blur(20px) saturate(0.8);
      "
    />
    <Dialog.Content class="tonus-token-sheet">
      <div
        style="
          position: relative;
          padding: 32px 34px 28px;
          border-radius: 22px;
          background: rgba(20, 20, 24, 0.85);
          backdrop-filter: blur(40px) saturate(1.2);
          -webkit-backdrop-filter: blur(40px) saturate(1.2);
          border: 1px solid var(--color-border-soft);
          box-shadow:
            0 32px 80px rgba(0, 0, 0, 0.6),
            0 0 0 1px rgba(255, 255, 255, 0.04),
            inset 0 1px 0 rgba(255, 255, 255, 0.06);
        "
      >
        <button
          type="button"
          onclick={close}
          aria-label="Schließen"
          class="absolute transition-colors"
          style="
            top: 16px;
            right: 16px;
            padding: 8px;
            border-radius: 999px;
            background: transparent;
            border: none;
            color: var(--color-fg-secondary);
            cursor: pointer;
          "
          onmouseenter={(e) =>
            (e.currentTarget.style.background = 'rgba(255, 255, 255, 0.06)')}
          onmouseleave={(e) => (e.currentTarget.style.background = 'transparent')}
        >
          <X size={14} strokeWidth={1.5} />
        </button>

        <div class="flex items-center gap-2 mb-2">
          <KeyRound size={13} strokeWidth={1.5} style="color: {accent};" />
          <Dialog.Title>
            {#snippet child({ props }: { props: Record<string, unknown> })}
              <div
                {...props}
                class="font-semibold uppercase"
                style="font-size: 11px; letter-spacing: 0.24em; color: {accent};"
              >
                Authentifizieren
              </div>
            {/snippet}
          </Dialog.Title>
        </div>

        <Dialog.Description>
          {#snippet child({ props }: { props: Record<string, unknown> })}
            <div
              {...props}
              style="
                font-family: var(--font-display);
                font-size: 22px;
                font-weight: 500;
                letter-spacing: -0.015em;
                color: var(--color-fg-primary);
                line-height: 1.2;
                margin-bottom: 6px;
              "
            >
              Browser ↔ Backend
            </div>
          {/snippet}
        </Dialog.Description>

        <p
          style="
            font-size: 12.5px;
            color: var(--color-fg-secondary);
            line-height: 1.55;
            margin: 0 0 20px;
          "
        >
          Bearer-Token aus
          <code
            style="font-family: var(--font-mono); color: {accent}; font-size: 12px;"
            >backend/.env</code
          >. Bleibt in deinem Browser, wird nie ans Backend zurückgespiegelt.
        </p>

        <form
          onsubmit={(e) => {
            e.preventDefault();
            save();
          }}
        >
          <label
            for="tonus-token-sheet-input"
            class="block uppercase"
            style="
              font-size: 10.5px;
              letter-spacing: 0.18em;
              color: var(--color-fg-tertiary);
              margin-bottom: 8px;
            "
          >
            API-Token
          </label>
          <input
            id="tonus-token-sheet-input"
            name="tonus-api-token"
            type="password"
            bind:value
            autocomplete="current-password"
            spellcheck="false"
            class="w-full outline-none transition-colors"
            style="
              background: rgba(0, 0, 0, 0.3);
              border: 1px solid var(--color-border-soft);
              border-radius: 14px;
              color: var(--color-fg-primary);
              font-family: var(--font-mono);
              font-size: 13px;
              padding: 12px 16px;
              letter-spacing: 0.04em;
            "
            placeholder="ttkn_•••••••••••••••••••••••••"
          />

          <div class="flex items-center gap-3 mt-6">
            <button
              type="submit"
              disabled={!value.trim()}
              class="inline-flex items-center gap-1.5 transition-transform disabled:opacity-40"
              style="
                padding: 10px 22px;
                border-radius: 999px;
                font-size: 12px;
                font-weight: 600;
                letter-spacing: 0.04em;
                text-transform: uppercase;
                background: {accent};
                color: #0a0a0c;
                border: none;
                box-shadow: 0 8px 24px {accent}40;
              "
            >
              {#if saved}
                <Check size={13} strokeWidth={2} />
                gespeichert
              {:else}
                speichern
              {/if}
            </button>
            <span
              style="
                font-size: 11px;
                color: var(--color-fg-tertiary);
                font-family: var(--font-mono);
                letter-spacing: 0.04em;
              "
            >
              ↵ zum Speichern · Esc zum Schließen
            </span>
          </div>
        </form>
      </div>
    </Dialog.Content>
  </Dialog.Portal>
</Dialog.Root>

<style>
  :global(.tonus-token-overlay[data-state='open']) {
    animation: tonus-token-fade-in 0.2s ease-out;
  }
  :global(.tonus-token-sheet) {
    position: fixed;
    inset: 0;
    z-index: 100;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 24px;
    pointer-events: none;
  }
  :global(.tonus-token-sheet > div) {
    pointer-events: auto;
    max-width: 520px;
    width: 100%;
  }
  :global(.tonus-token-sheet[data-state='open']) {
    animation: tonus-token-rise 0.3s cubic-bezier(0.16, 1, 0.3, 1);
  }
  :global(.tonus-token-sheet[data-state='closed']) {
    animation: tonus-token-fall 0.2s ease-in;
  }
  @keyframes tonus-token-fade-in {
    from {
      opacity: 0;
    }
    to {
      opacity: 1;
    }
  }
  @keyframes tonus-token-rise {
    from {
      opacity: 0;
      transform: translateY(12px) scale(0.98);
    }
    to {
      opacity: 1;
      transform: translateY(0) scale(1);
    }
  }
  @keyframes tonus-token-fall {
    from {
      opacity: 1;
      transform: translateY(0);
    }
    to {
      opacity: 0;
      transform: translateY(8px);
    }
  }
  @media (prefers-reduced-motion: reduce) {
    :global(.tonus-token-overlay[data-state='open']),
    :global(.tonus-token-sheet[data-state='open']),
    :global(.tonus-token-sheet[data-state='closed']) {
      animation: none;
    }
  }
</style>
