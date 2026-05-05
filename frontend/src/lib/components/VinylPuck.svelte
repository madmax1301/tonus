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
  class="tonus-vinyl-puck"
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
    background:
      radial-gradient(circle at 50% 50%, {$flashAccent ?? goldAccent} 0 6px, #18120c 6px 8px, transparent 8px),
      repeating-radial-gradient(circle at 50% 50%, #0a0a0c 0 1px, #161618 1px 3px),
      #0a0a0c;
    box-shadow:
      0 12px 40px rgba(0, 0, 0, 0.6),
      0 0 0 1px rgba(255, 255, 255, 0.06){$flashAccent ? `, 0 0 60px ${$flashAccent}` : ''};
    display: flex;
    align-items: center;
    justify-content: center;
    transition: box-shadow 0.4s ease;
    will-change: transform;
  "
>
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
  /* spin only the outer disc + grooves; the counter label stays upright
     via reverse-spin on the inner disc.

     Wichtig: animation-duration ändert sich NICHT bei :hover. Vorher
     hatte das Rule die Duration auf 2s reduziert, was bei Mobile-Touch
     (sticky-hover) die Animation beim End-of-touch neu starten ließ —
     der User sah den Vinyl "wackeln statt drehen" weil die Animation
     pro Touch von Frame 0 startete. Jetzt konstant 4s. */
  .tonus-vinyl-puck {
    animation: tonus-puck-spin 4s linear infinite;
  }
  .tonus-vinyl-puck__disc {
    /* counter readable trotz parent-spin — gleiche Geschwindigkeit
       in Gegenrichtung. */
    animation: tonus-puck-spin 4s linear infinite reverse;
  }
  /* from-Keyframe explizit — manche Browser starten die Animation
     anders interpoliert wenn from fehlt und initial transform nicht
     identity ist (z.B. parent-transformed). */
  @keyframes tonus-puck-spin {
    from {
      transform: rotate(0deg);
    }
    to {
      transform: rotate(360deg);
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .tonus-vinyl-puck,
    .tonus-vinyl-puck__disc {
      animation: none;
    }
  }
</style>
