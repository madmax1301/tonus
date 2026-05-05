<script lang="ts">
  /**
   * Vinyl-Puck unten rechts — empfangs-Ziel der Fly-to-Queue-Animation.
   *
   * 96×96 Kreis mit konzentrischen Vinyl-Rillen (repeating-radial-gradient)
   * und Center-Label (innere 30%) mit Queue-Counter in IBM Plex Mono.
   * Spinning-Animation (4 s linear infinite) ist immer aktiv solange Counter > 0.
   *
   * Bei flash-Akzent (gesetzt via flashAccent-Store kurz nach Cover-Ankunft):
   *   800 ms box-shadow: 0 0 60px <accent>
   *   Center-Label-BG wechselt zur Akzentfarbe
   *
   * Click leitet auf /queue weiter — Puck ist gleichzeitig "Statusanzeige"
   * und "Queue-Shortcut".
   *
   * `data-vinyl-puck` ist der Marker, den fly-to-queue.ts via querySelector
   * findet, um die Ziel-Position zu bestimmen.
   */
  import { goto } from '$app/navigation';
  import { base } from '$app/paths';
  import { queueCount, flashAccent } from '$lib/fly-to-queue';
  import { tint, DEFAULT_HUE } from '$lib/accent';

  const goldAccent = tint(DEFAULT_HUE);
  const goldDeep = tint(DEFAULT_HUE, 0.7);

  function gotoQueue() {
    goto(`${base}/queue`);
  }
</script>

<button
  type="button"
  data-vinyl-puck
  onclick={gotoQueue}
  class="tonus-vinyl-puck-shell"
  aria-label="Queue öffnen — {$queueCount} Jobs"
  style="
    position: fixed;
    right: 24px;
    bottom: 24px;
    z-index: 50;
    width: 96px;
    height: 96px;
    border-radius: 50%;
    border: none;
    cursor: pointer;
    padding: 0;
    background: transparent;
    box-shadow:
      0 12px 40px rgba(0, 0, 0, 0.6),
      0 0 0 1px rgba(255, 255, 255, 0.06){$flashAccent ? `, 0 0 60px ${$flashAccent}` : ''};
    display: flex;
    align-items: center;
    justify-content: center;
    transition: box-shadow 0.4s ease;
    contain: layout paint;
  "
>
  <!-- Spinning grooves layer — pure transform animation, eigene
       Compositor-Layer durch will-change + translate3d in Keyframes.
       Bleibt smooth auch wenn Main-Thread durch große Listen blockt. -->
  <span
    class="tonus-vinyl-puck"
    aria-hidden="true"
    style="
      position: absolute;
      inset: 0;
      border-radius: 50%;
      background:
        radial-gradient(circle at 50% 50%, {$flashAccent ?? goldAccent} 0 6px, #18120c 6px 8px, transparent 8px),
      repeating-radial-gradient(circle at 50% 50%, #0a0a0c 0 1px, #161618 1px 3px),
      #0a0a0c;
      will-change: transform;
    "
  ></span>

  <span
    class="tonus-vinyl-puck__disc"
    style="
      position: absolute;
      inset: 30%;
      border-radius: 50%;
      background: radial-gradient(circle, {$flashAccent ?? goldDeep}, oklch(22% 0.05 30));
      display: flex;
      align-items: center;
      justify-content: center;
      transition: background 0.4s ease;
      will-change: transform;
    "
  >
    <span
      class="tonus-vinyl-puck__count"
      style="
        font-family: var(--font-mono);
        font-size: 13px;
        color: #0a0a0c;
        font-weight: 700;
        letter-spacing: -0.02em;
        background: rgba(255, 255, 255, 0.85);
        padding: 2px 8px;
        border-radius: 4px;
        line-height: 1;
        font-variant-numeric: tabular-nums;
      "
    >
      {$queueCount}
    </span>
  </span>
</button>

<!-- Mini-Label "QUEUE · live" links neben dem Puck — orientiert sich an
     der Spec; gibt dem Puck Kontext, ohne zusätzliche Surface zu kosten. -->
<div
  aria-hidden="true"
  style="
    position: fixed;
    right: 130px;
    bottom: 54px;
    z-index: 50;
    font-size: 10.5px;
    color: var(--color-fg-secondary);
    font-family: var(--font-mono);
    letter-spacing: 0.1em;
    text-transform: uppercase;
    text-align: right;
    pointer-events: none;
  "
>
  Queue<br />
  <span style="color: {$flashAccent ?? goldAccent}; font-weight: 600;">● live</span>
</div>

<style>
  /* Architektur: outer button (.tonus-vinyl-puck-shell) ist statisch und
     trägt den box-shadow (mit transition). Innerer .tonus-vinyl-puck
     trägt die Grooves und rotiert — pure transform-only animation, läuft
     auf Compositor-Thread. Damit bleibt die Rotation smooth auch wenn
     der Main-Thread durch große Queue-Listen (15k+ Items) blockt.

     Inner .tonus-vinyl-puck__disc spinnt reverse damit die Counter-
     Zahl aufrecht bleibt. */

  .tonus-vinyl-puck-shell:hover {
    /* dezenter Hover-Hinweis ohne animation-duration-Switch (der hat
       vorher beim Mobile-Touch sticky-hover die Animation neu gestartet) */
    box-shadow:
      0 16px 48px rgba(0, 0, 0, 0.7),
      0 0 0 1px rgba(255, 255, 255, 0.1) !important;
  }

  .tonus-vinyl-puck {
    animation: tonus-puck-spin 4s linear infinite;
  }
  .tonus-vinyl-puck__disc {
    animation: tonus-puck-spin 4s linear infinite reverse;
  }

  /* Keyframes mit translate3d(0,0,0) zwingen den Browser, das Element auf
     eine eigene Compositor-Layer zu heben — UND zwar für die Dauer der
     gesamten Animation, nicht nur als initial-state. Pure-transform =
     compositor-only = unbeeinflusst von Main-Thread-Lag. */
  @keyframes tonus-puck-spin {
    from {
      transform: translate3d(0, 0, 0) rotate(0deg);
    }
    to {
      transform: translate3d(0, 0, 0) rotate(360deg);
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .tonus-vinyl-puck,
    .tonus-vinyl-puck__disc {
      animation: none;
    }
  }
</style>
