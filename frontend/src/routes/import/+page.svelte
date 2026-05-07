<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { browser } from '$app/environment';
  import { tweened } from 'svelte/motion';
  import { cubicOut } from 'svelte/easing';
  import {
    importApi,
    providersApi,
    searchApi,
    downloadApi,
    ApiError,
    type CsvImportStatus,
    type CsvImportResult,
    type MetadataProvidersResponse,
    type Track
  } from '$lib/api';
  import { defaultProvider, defaultLocation, defaultFormat, defaultQuality } from '$lib/preferences';
  import { get } from 'svelte/store';
  import CinemaBackdrop from '$lib/components/CinemaBackdrop.svelte';
  import VinylWithCover from '$lib/components/VinylWithCover.svelte';
  import ProgressLine from '$lib/components/ProgressLine.svelte';
  import AlbumArt from '$lib/components/AlbumArt.svelte';
  import { tint, DEFAULT_HUE } from '$lib/accent';
  import { t } from '$lib/i18n';
  import { flyToQueue } from '$lib/fly-to-queue';
  import { Upload, Loader2, Download, FileText, X, Search } from 'lucide-svelte';

  // localStorage-Key für die Job-Resume-Logik. Wenn der User reloadet,
  // ein neues Tab öffnet oder Tonus für 5 Min schließt, wird der laufende
  // Import-Job hier referenziert und beim Mount wieder aufgenommen. Der
  // eigentliche Job läuft persistent im Backend (SQLite + Worker-Thread)
  // — der localStorage-Key sagt dem Frontend nur, *welchen* Job es
  // beobachten soll. Verschwindet nach Job-Abschluss / Reset.
  const ACTIVE_CSV_KEY = 'tonus_csv_active_job';

  // ── Provider ────────────────────────────────────────────
  let provider = $state<string>('');
  let providersData = $state<MetadataProvidersResponse | null>(null);

  (async () => {
    try {
      providersData = await providersApi.list();
      const userPref = get(defaultProvider);
      provider = userPref || providersData.default;
    } catch {
      /* sheet handles auth */
    }
  })();

  // ── CSV ──────────────────────────────────────────────────
  let csvText = $state('');
  let csvJobId = $state<string | null>(null);
  // Original-Filename, wenn der User eine Datei gedroppt hat. Bei Text-Paste
  // bleibt's null und wir fallen zurück auf "Job · csv-1234" als Tab-Label.
  let csvFilename = $state<string | null>(null);
  let csvStatus = $state<CsvImportStatus | null>(null);
  let csvResult = $state<CsvImportResult | null>(null);
  let csvBusy = $state(false);
  let csvError = $state<string | null>(null);
  let csvQueueAllBusy = $state(false);
  let csvQueueAllResult = $state<string | null>(null);
  let csvLoadMoreBusy = $state(false);
  let csvExportBusy = $state(false);
  let csvExportProgress = $state<{ loaded: number; total: number } | null>(null);
  const PAGE_SIZE = 200;

  // Viewport-aware Vinyl-size — Phone shrinkt das 260px-Cover (mit Disc-Offset
  // ~403px Gesamt) sonst überläuft. SSR-safe via $effect (clientside-only).
  let vinylSize = $state(260);
  $effect(() => {
    if (!browser) return;
    const update = () => {
      vinylSize = window.innerWidth < 640 ? 170 : 260;
    };
    update();
    window.addEventListener('resize', update);
    return () => window.removeEventListener('resize', update);
  });

  // Smooth-Counter mit svelte/motion `tweened` — Backend liefert alle 5 Calls
  // einen Status-Update, das Frontend pollt alle 1500 ms. Ohne Easing wären
  // das visuelle Sprünge (0 → 34 → 65 → 105). Tweened interpoliert zwischen
  // den Snapshots in 800 ms cubicOut, sodass die Zahlen sichtbar hochlaufen.
  // tweenProcessed: für die "X / 777"-Anzeige + Bar-Progress.
  // tweenFound / tweenNotFound: für die grüne/rote Live-Counter unten.
  const tweenProcessed = tweened(0, { duration: 800, easing: cubicOut });
  const tweenFound = tweened(0, { duration: 800, easing: cubicOut });
  const tweenNotFound = tweened(0, { duration: 800, easing: cubicOut });

  let csvPollTimer: ReturnType<typeof setInterval> | null = null;

  function stopCsvPoll() {
    if (csvPollTimer) {
      clearInterval(csvPollTimer);
      csvPollTimer = null;
    }
  }
  onDestroy(stopCsvPoll);

  async function startCsv() {
    if (!csvText.trim()) return;
    csvBusy = true;
    csvError = null;
    csvResult = null;
    csvQueueAllResult = null;
    // Tweens hart zurücksetzen — `set` ohne 2. Arg animiert nicht, sondern
    // springt direkt auf 0. Sonst sieht der User noch die alte Match-Rate
    // vom vorigen Job durchschimmern, bis das erste Polling-Update kommt.
    tweenProcessed.set(0, { duration: 0 });
    tweenFound.set(0, { duration: 0 });
    tweenNotFound.set(0, { duration: 0 });
    try {
      const r = await importApi.startCsv(
        csvText,
        provider || undefined,
        undefined,
        csvFilename || undefined
      );
      csvJobId = r.job_id;
      csvStatus = {
        status: 'queued',
        total: r.total ?? 0,
        processed: 0,
        found: 0,
        not_found: 0,
        message: r.message,
        filename: csvFilename
      };
      // Job-ID in localStorage hinterlegen, damit ein Page-Reload den
      // Import-Status weiterbeobachten kann statt ihn zu vergessen.
      if (browser) {
        try {
          localStorage.setItem(ACTIVE_CSV_KEY, r.job_id);
        } catch {
          /* private mode / quota — silent */
        }
      }
      pollCsv();
    } catch (err) {
      csvError = err instanceof Error ? err.message : 'CSV-Import fehlgeschlagen';
    } finally {
      csvBusy = false;
    }
  }

  function pollCsv() {
    if (!csvJobId) return;
    stopCsvPoll();
    const tick = async () => {
      if (!csvJobId) return;
      try {
        const s = await importApi.status(csvJobId);
        csvStatus = s;
        // Tweens auf neue Backend-Werte schicken — animiert smooth dorthin
        // (cubicOut, 800 ms). Setzt csvFilename auf den Backend-Wert sobald
        // verfügbar, damit nach einem Reload der Tab-Title stimmt.
        tweenProcessed.set(s.processed);
        tweenFound.set(s.found);
        tweenNotFound.set(s.not_found);
        if (s.filename && !csvFilename) {
          csvFilename = s.filename;
        }
        if (s.status === 'completed') {
          stopCsvPoll();
          csvResult = await importApi.result(csvJobId, 0, PAGE_SIZE);
          // Job ist durch — localStorage kann gelöscht werden, der nächste
          // Reload landet sauber im Drop-Zone-Zustand.
          if (browser) {
            try {
              localStorage.removeItem(ACTIVE_CSV_KEY);
            } catch {
              /* noop */
            }
          }
        } else if (s.status === 'error') {
          stopCsvPoll();
          csvError = s.message ?? 'CSV-Import-Fehler';
          if (browser) {
            try {
              localStorage.removeItem(ACTIVE_CSV_KEY);
            } catch {
              /* noop */
            }
          }
        }
      } catch (err) {
        // 404 = Job-ID aus localStorage zeigt auf einen Job, der nicht mehr
        // existiert (Backend-DB-Reset, manuelles cleanup). Sauber abbrechen
        // statt ewig dagegen zu pollen.
        if (err instanceof ApiError && err.status === 404) {
          stopCsvPoll();
          csvJobId = null;
          csvStatus = null;
          csvFilename = null;
          if (browser) {
            try {
              localStorage.removeItem(ACTIVE_CSV_KEY);
            } catch {
              /* noop */
            }
          }
        }
        /* sonst: Netzwerk-Glitch o.ä. — weiterpollen */
      }
    };
    tick();
    csvPollTimer = setInterval(tick, 1500);
  }

  // ── Resume-On-Reload ─────────────────────────────────────
  // Beim Page-Mount: prüfen ob ein Job in localStorage hängt und ggf.
  // den Status sofort fetchen. Wenn der Job noch processing/queued ist,
  // pollen wir weiter. Wenn er schon completed ist, laden wir das Result
  // und zeigen dem User die fertige Liste — er sieht also sofort wo der
  // letzte Stand war, statt einen leeren Drop-Zone-Screen.
  onMount(async () => {
    if (!browser) return;
    let resumeId: string | null = null;
    try {
      resumeId = localStorage.getItem(ACTIVE_CSV_KEY);
    } catch {
      return;
    }
    if (!resumeId) return;
    try {
      const s = await importApi.status(resumeId);
      csvJobId = resumeId;
      csvStatus = s;
      if (s.filename) csvFilename = s.filename;
      // Tweens direkt auf den Resume-Wert setzen (kein Animations-Sprung
      // von 0 → 700, das wäre verwirrend).
      tweenProcessed.set(s.processed, { duration: 0 });
      tweenFound.set(s.found, { duration: 0 });
      tweenNotFound.set(s.not_found, { duration: 0 });
      if (s.status === 'completed') {
        csvResult = await importApi.result(resumeId, 0, PAGE_SIZE);
        try {
          localStorage.removeItem(ACTIVE_CSV_KEY);
        } catch {
          /* noop */
        }
      } else if (s.status === 'error') {
        csvError = s.message ?? 'CSV-Import-Fehler';
        try {
          localStorage.removeItem(ACTIVE_CSV_KEY);
        } catch {
          /* noop */
        }
      } else {
        // queued/processing → weiterpollen
        pollCsv();
      }
    } catch (err) {
      // 404 = Job ist weg (Backend-Reset etc.) — Local-Reference löschen
      // und auf neue Eingabe warten.
      if (err instanceof ApiError && err.status === 404) {
        try {
          localStorage.removeItem(ACTIVE_CSV_KEY);
        } catch {
          /* noop */
        }
      }
      /* andere Fehler: ignorieren, beim nächsten Versuch geht's vielleicht */
    }
  });

  async function queueAllMatched(ev?: MouseEvent) {
    if (!csvJobId) return;
    csvQueueAllBusy = true;
    csvQueueAllResult = null;
    // Bulk-Queue löst eine kurze Burst-Animation aus: pro N Tracks
    // ein Cover-Klon, gestaffelt über 250 ms damit's wie ein "Schwarm"
    // wirkt statt chaotisches Geblitze. Source ist der Queue-Button
    // selbst (kein Cover verfügbar — wir nehmen das Result-Hero-Cover
    // wenn da, sonst den Button).
    const btnEl = ev ? (ev.currentTarget as HTMLElement) : null;
    try {
      const r = await importApi.queueAll(csvJobId, {
        location: $defaultLocation,
        provider: provider || undefined
      });
      const queued = r.queued ?? 0;
      const skipped = r.skipped ?? 0;
      csvQueueAllResult =
        skipped > 0 ? `${queued} queued, ${skipped} schon da` : `${queued} queued`;

      // Bis zu 5 Klone hintereinander schicken — repräsentative Burst-
      // Animation. Die ersten 5 matched Tracks liefern die Cover-URLs,
      // damit's nicht alle gleich aussehen. Counter wird vom Backend-
      // Polling synchronisiert (alle 5 s), Animation darf optimistisch
      // bumpen ohne Sorge um Drift.
      const sources = (csvResult?.matched ?? []).slice(0, 5);
      const fallbackSrc = sources[0]?.track?.album_art ?? null;
      const burst = Math.min(queued, 5);
      for (let i = 0; i < burst; i++) {
        const src = sources[i]?.track?.album_art ?? fallbackSrc;
        const startEl = btnEl ?? document.body;
        // Stagger 80 ms zwischen den Klonen — alle laufen parallel,
        // aber leicht versetzt für den "Schwarm"-Effekt.
        window.setTimeout(() => {
          flyToQueue(startEl, src, accent, 48);
        }, i * 80);
      }
    } catch (err) {
      csvQueueAllResult = err instanceof Error ? err.message : 'Fehler beim Queuen';
    } finally {
      csvQueueAllBusy = false;
    }
  }

  async function loadMoreUnmatched() {
    if (!csvJobId || !csvResult) return;
    csvLoadMoreBusy = true;
    try {
      const next = await importApi.result(csvJobId, csvResult.unmatched.length, PAGE_SIZE);
      csvResult = {
        ...csvResult,
        unmatched: [...csvResult.unmatched, ...next.unmatched]
      };
    } catch {
      /* noop */
    } finally {
      csvLoadMoreBusy = false;
    }
  }

  function csvEscape(s: string): string {
    if (s == null) return '';
    const v = String(s);
    if (/[",\n;\r]/.test(v)) return `"${v.replace(/"/g, '""')}"`;
    return v;
  }

  async function exportUnmatched() {
    if (!csvJobId || !csvResult) return;
    csvExportBusy = true;
    csvExportProgress = { loaded: csvResult.unmatched.length, total: csvResult.not_found };
    try {
      const collected = [...csvResult.unmatched];
      const target = csvResult.not_found;
      const PAGE = 500;
      while (collected.length < target) {
        const r = await importApi.result(csvJobId, collected.length, PAGE);
        if (!r.unmatched.length) break;
        collected.push(...r.unmatched);
        csvExportProgress = { loaded: collected.length, total: target };
      }

      const header = 'artist;title;original';
      const lines = collected.map((u) =>
        [
          csvEscape(u.requested_artist || ''),
          csvEscape(u.requested_title || ''),
          csvEscape(u.original || '')
        ].join(';')
      );
      const csv = '﻿' + [header, ...lines].join('\n');
      const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `tonus-unmatched-${csvJobId}.csv`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch {
      /* User can retry */
    } finally {
      csvExportBusy = false;
      csvExportProgress = null;
    }
  }

  function resetCsv() {
    stopCsvPoll();
    csvJobId = null;
    csvFilename = null;
    csvStatus = null;
    csvResult = null;
    csvError = null;
    csvText = '';
    csvQueueAllResult = null;
    csvExportProgress = null;
    tweenProcessed.set(0, { duration: 0 });
    tweenFound.set(0, { duration: 0 });
    tweenNotFound.set(0, { duration: 0 });
    if (browser) {
      try {
        localStorage.removeItem(ACTIVE_CSV_KEY);
      } catch {
        /* noop */
      }
    }
  }

  async function onCsvFile(e: Event) {
    const input = e.currentTarget as HTMLInputElement;
    const f = input.files?.[0];
    if (!f) return;
    csvText = await f.text();
    // Filename merken — wird beim startCsv mit ans Backend geschickt und
    // landet als Tab-Label "playlist.csv" statt "Job · csv-1234".
    csvFilename = f.name;
    input.value = '';
  }

  // ── Per-Row Re-Check für Unmatched ──────────────────────
  // Sucht den Track mit derselben /api/search-Funktion wie der Library-Screen.
  // Wenn ein Treffer kommt → CSV-Match-Worker hat ihn übersehen (Bug oder
  // Query-Format-Drift). Wenn 0 Treffer → echter Deezer-Miss.
  type RecheckState = {
    loading: boolean;
    results?: Track[];
    error?: string;
    queueState?: Record<string, 'queued' | 'done' | 'exists' | 'error'>;
  };
  let recheck = $state<Record<number, RecheckState>>({});

  async function recheckRow(index: number, artist: string, title: string) {
    const query = `${artist} ${title}`.trim();
    if (!query) return;
    recheck = { ...recheck, [index]: { loading: true, queueState: {} } };
    try {
      const results = await searchApi.tracks(query, provider || undefined, 5);
      recheck = {
        ...recheck,
        [index]: { loading: false, results, queueState: {} }
      };
    } catch (err) {
      recheck = {
        ...recheck,
        [index]: {
          loading: false,
          results: [],
          error: err instanceof Error ? err.message : 'Suche fehlgeschlagen',
          queueState: {}
        }
      };
    }
  }

  async function queueRecheckTrack(index: number, track: Track, ev?: MouseEvent) {
    const cur = recheck[index];
    if (!cur) return;
    const qs = { ...(cur.queueState ?? {}), [track.id]: 'queued' as const };
    recheck = { ...recheck, [index]: { ...cur, queueState: qs } };
    let coverEl: HTMLElement | null = null;
    if (ev) {
      const btn = ev.currentTarget as HTMLElement;
      const row = btn.closest<HTMLElement>('[data-recheck-track]');
      coverEl = row?.querySelector<HTMLElement>('[data-cover]') ?? null;
    }
    try {
      await downloadApi.start(track.id, {
        location: $defaultLocation,
        provider: provider || undefined,
        format: $defaultFormat || undefined,
        quality: $defaultQuality || undefined
      });
      const cur2 = recheck[index];
      if (!cur2) return;
      recheck = {
        ...recheck,
        [index]: {
          ...cur2,
          queueState: { ...(cur2.queueState ?? {}), [track.id]: 'done' }
        }
      };
      if (coverEl) {
        flyToQueue(coverEl, track.album_art ?? null, accent, coverEl.offsetWidth);
      }
    } catch (err) {
      const cur2 = recheck[index];
      if (!cur2) return;
      const kind: 'exists' | 'error' =
        err instanceof ApiError && err.status === 409 ? 'exists' : 'error';
      recheck = {
        ...recheck,
        [index]: {
          ...cur2,
          queueState: { ...(cur2.queueState ?? {}), [track.id]: kind }
        }
      };
    }
  }

  function fmtDur(ms: number): string {
    if (!ms) return '';
    const s = Math.round(ms / 1000);
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  }

  // ── Drag & Drop ──────────────────────────────────────────
  let dragOver = $state(false);
  let dragDepth = 0;

  function onDragEnter(e: DragEvent) {
    if (!e.dataTransfer?.types.includes('Files')) return;
    e.preventDefault();
    dragDepth++;
    dragOver = true;
  }
  function onDragOver(e: DragEvent) {
    if (!e.dataTransfer?.types.includes('Files')) return;
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
  }
  function onDragLeave(e: DragEvent) {
    e.preventDefault();
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) dragOver = false;
  }
  async function onDrop(e: DragEvent) {
    e.preventDefault();
    dragDepth = 0;
    dragOver = false;
    const f = e.dataTransfer?.files?.[0];
    if (!f) return;
    csvText = await f.text();
  }

  // Bar-Progress aus dem tweened processed-Counter — damit die Bar
  // synchron zum smooth Counter läuft, nicht zu den Backend-Snapshots.
  const csvProgress = $derived(
    csvStatus && csvStatus.total
      ? Math.round(($tweenProcessed / csvStatus.total) * 100)
      : 0
  );

  // Accent — Import hat keinen Cover-Hue, daher konstant Default-Gold.
  const accent = $derived(tint(DEFAULT_HUE));
  const accentSoft = $derived(tint(DEFAULT_HUE, 0.95));

  const lineCount = $derived(csvText.split(/\r?\n/).filter((l) => l.trim()).length);
</script>

<CinemaBackdrop hue={DEFAULT_HUE} />

<section class="relative z-10 mx-auto max-w-[1180px] w-full" style="padding: clamp(20px, 4vw, 40px) clamp(14px, 4vw, 36px) clamp(28px, 5vw, 50px);">
  <!-- ─── Editorial Hero ───────────────────────────────────── -->
  <div
    class="grid items-center tonus-import-hero"
    style="grid-template-columns: 1.3fr 1fr; gap: 48px; margin-bottom: clamp(28px, 5vw, 48px);"
  >
    <div>
      <div
        class="font-semibold uppercase"
        style="
          font-size: 11px;
          letter-spacing: 0.24em;
          color: {accent};
          margin-bottom: 14px;
        "
      >
        {$t('import.eyebrow')}
      </div>
      <h1
        class="font-semibold m-0"
        style="
          font-family: var(--font-display);
          font-size: clamp(30px, 7vw, 48px);
          font-weight: 600;
          line-height: 0.95;
          letter-spacing: -0.035em;
        "
      >
        {$t('import.title.before')}<br />
        <em style="color: {accent}; font-weight: 400; font-style: italic;"
          >{$t('import.title.italic')}</em
        >
      </h1>
      <p
        style="
          font-size: 14px;
          color: var(--color-fg-secondary);
          max-width: 440px;
          margin-top: 18px;
          line-height: 1.6;
        "
      >
        {$t('import.description')}
      </p>
    </div>
    <div class="flex justify-center tonus-import-vinyl">
      <VinylWithCover
        src={null}
        alt=""
        artist="Liste"
        year={lineCount > 0 ? lineCount : ''}
        size={vinylSize}
        spinning={!!csvJobId && csvStatus?.status !== 'completed'}
      />
    </div>
  </div>

  <!-- ─── Mode strip — Provider als underline-Pill (analog Library) ─── -->
  <div
    class="flex items-center"
    style="
      gap: 24px;
      font-size: 13px;
      margin-bottom: 24px;
      border-bottom: 1px solid var(--color-border-soft);
      padding-bottom: 14px;
    "
  >
    <div
      class="relative inline-flex items-center"
      style="
        color: var(--color-fg-primary);
        font-weight: 500;
        padding-bottom: 14px;
        margin-bottom: -14px;
        border-bottom: 2px solid {accent};
      "
    >
      CSV
    </div>
    {#if csvJobId}
      <div class="text-[12px] truncate" style="color: var(--color-fg-tertiary); max-width: 360px;">
        {#if csvFilename}
          <span title={csvFilename}>{csvFilename}</span>
        {:else}
          Job · <span class="font-mono" style="font-family: var(--font-mono);">{csvJobId.slice(0, 8)}</span>
        {/if}
      </div>
    {/if}
    {#if providersData}
      <select
        bind:value={provider}
        disabled={!!csvJobId}
        class="ml-auto text-[11px] px-2 py-1 rounded-md outline-none disabled:opacity-60"
        style="
          background: rgba(255, 255, 255, 0.02);
          border: 1px solid var(--color-border-soft);
          color: var(--color-fg-tertiary);
        "
      >
        {#each providersData.providers.filter((p) => p.configured) as p}
          <option value={p.id}>{p.label} · Provider</option>
        {/each}
      </select>
    {/if}
  </div>

  {#if !csvJobId}
    <!-- ─── TuneMyMusic-Helper-Card (Phase I) ────────────────── -->
    <!-- Erklärt User wie er Spotify/Deezer-Playlists ohne OAuth nach Tonus  -->
    <!-- bekommt: tunemymusic.com → CSV-Export → hier hochladen.              -->
    <div
      class="relative overflow-hidden tonus-fadein"
      style="
        background: rgba(20, 20, 24, 0.4);
        backdrop-filter: blur(28px) saturate(1.15);
        -webkit-backdrop-filter: blur(28px) saturate(1.15);
        border: 1px solid var(--color-border-soft);
        border-radius: 18px;
        padding: clamp(16px, 3vw, 22px);
        margin-bottom: 18px;
      "
    >
      <div class="flex items-start justify-between gap-3 mb-3 flex-wrap">
        <div>
          <div
            class="font-semibold uppercase"
            style="font-size: 10px; letter-spacing: 0.22em; color: {accent};"
          >
            {$t('import.tunemymusic.eyebrow')}
          </div>
          <div
            class="mt-1.5 font-medium"
            style="font-family: var(--font-display); font-size: 18px; letter-spacing: -0.01em; color: var(--color-fg-primary);"
          >
            {$t('import.tunemymusic.title')}
          </div>
        </div>
        <a
          href="https://www.tunemymusic.com/"
          target="_blank"
          rel="noopener noreferrer"
          class="inline-flex items-center gap-1.5 text-[12px] font-medium tabular-nums px-3 py-1.5"
          style="
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid var(--color-border-soft);
            border-radius: 999px;
            color: var(--color-fg-primary);
            text-decoration: none;
          "
        >
          tunemymusic.com →
        </a>
      </div>
      <ol class="text-[13px] space-y-1.5 ml-4" style="color: var(--color-fg-secondary); list-style: decimal;">
        <li>{$t('import.tunemymusic.step1')}</li>
        <li>{$t('import.tunemymusic.step2')}</li>
        <li>{$t('import.tunemymusic.step3')}</li>
      </ol>
      <div class="mt-3 text-[11px]" style="color: var(--color-fg-tertiary);">
        {$t('import.tunemymusic.limit_note')}
      </div>
    </div>

    <!-- ─── Glass Drop-Zone ─────────────────────────────────── -->
    <div
      role="region"
      aria-label="CSV-Eingabe"
      class="relative overflow-hidden tonus-fadein"
      style="
        background: rgba(20, 20, 24, 0.5);
        backdrop-filter: blur(40px) saturate(1.2);
        -webkit-backdrop-filter: blur(40px) saturate(1.2);
        border: 1px solid {dragOver ? accent : 'var(--color-border-soft)'};
        border-radius: 22px;
        padding: clamp(16px, 4vw, 28px);
        box-shadow: 0 24px 60px rgba(0, 0, 0, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.05);
        transition: border-color 0.18s ease;
      "
      ondragenter={onDragEnter}
      ondragover={onDragOver}
      ondragleave={onDragLeave}
      ondrop={onDrop}
    >
      <!-- Drag overlay -->
      {#if dragOver}
        <div
          class="absolute inset-0 z-10 flex items-center justify-center pointer-events-none"
          style="
            background: rgba(20, 20, 24, 0.85);
            border: 2px dashed {accent};
            border-radius: 22px;
            backdrop-filter: blur(8px);
          "
        >
          <div class="text-center">
            <Upload size={48} strokeWidth={1.2} style="color: {accent}; margin-inline: auto;" />
            <div
              class="font-semibold uppercase mt-3"
              style="font-size: 11px; letter-spacing: 0.24em; color: {accent};"
            >
              {$t('import.dropzone.drop_overlay')}
            </div>
          </div>
        </div>
      {/if}

      <!-- Header row: title + actions -->
      <div class="flex items-center justify-between flex-wrap gap-3 mb-4">
        <div>
          <div
            class="font-semibold uppercase"
            style="
              font-size: 11px;
              letter-spacing: 0.2em;
              color: var(--color-fg-tertiary);
            "
          >
            {$t('import.dropzone.eyebrow')}
          </div>
          <div
            class="mt-1"
            style="
              font-family: var(--font-display);
              font-size: 22px;
              font-weight: 500;
              letter-spacing: -0.015em;
              color: var(--color-fg-primary);
            "
          >
            {$t('import.dropzone.title')}
          </div>
        </div>
        <div class="flex items-center gap-2">
          <label
            class="inline-flex items-center gap-1.5 cursor-pointer transition-colors"
            style="
              background: rgba(255, 255, 255, 0.04);
              border: 1px solid var(--color-border-soft);
              color: var(--color-fg-secondary);
              padding: 6px 14px;
              border-radius: 999px;
              font-size: 11.5px;
            "
          >
            <Upload size={12} strokeWidth={1.5} />
            {$t('import.dropzone.choose_file')}
            <input
              type="file"
              accept=".csv,.tsv,.txt,text/*"
              onchange={onCsvFile}
              class="hidden"
            />
          </label>
          {#if csvText}
            <button
              type="button"
              onclick={() => (csvText = '')}
              class="inline-flex items-center gap-1.5 transition-colors"
              style="
                background: rgba(255, 255, 255, 0.02);
                border: 1px solid var(--color-border-soft);
                color: var(--color-fg-tertiary);
                padding: 6px 12px;
                border-radius: 999px;
                font-size: 11px;
              "
              aria-label="Zurücksetzen"
            >
              <X size={11} strokeWidth={1.5} />
              {$t('import.dropzone.clear')}
            </button>
          {/if}
        </div>
      </div>

      <textarea
        bind:value={csvText}
        rows="10"
        spellcheck="false"
        placeholder={'Künstler;Titel\nDaft Punk;Get Lucky\nQueen;Bohemian Rhapsody\n\noder eine Zeile pro Track als Freitext'}
        class="w-full px-4 py-3 outline-none resize-y"
        style="
          background: rgba(0, 0, 0, 0.25);
          border: 1px solid var(--color-border-soft);
          border-radius: 14px;
          color: var(--color-fg-primary);
          font-family: var(--font-mono);
          font-size: 12.5px;
          line-height: 1.55;
        "
      ></textarea>

      <!-- Footer row: action -->
      <div class="flex items-center justify-between flex-wrap gap-3 mt-4">
        <div class="text-[12px]" style="color: var(--color-fg-tertiary);">
          {#if lineCount > 0}
            <span style="color: {accent}; font-weight: 500;">{lineCount.toLocaleString('de-DE')}</span>
            <span> Zeile{lineCount === 1 ? '' : 'n'} bereit</span>
          {:else}
            {$t('import.dropzone.format_hint')} <code style="font-family: var(--font-mono); color: var(--color-fg-secondary);">Künstler;Titel</code>
          {/if}
        </div>
        <button
          onclick={startCsv}
          disabled={csvBusy || !csvText.trim()}
          class="inline-flex items-center gap-2 transition-opacity disabled:opacity-40"
          style="
            background: {accent};
            color: #1a1410;
            padding: 10px 22px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 600;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            box-shadow: 0 8px 20px rgba(200, 169, 106, 0.25);
          "
        >
          {#if csvBusy}
            <Loader2 size={13} class="animate-spin" />
            {$t('common.loading')}
          {:else}
            <FileText size={13} strokeWidth={2} />
            Import starten
          {/if}
        </button>
      </div>
      {#if csvError}
        <div
          class="mt-3 text-[12px]"
          style="color: var(--color-status-error);"
        >
          {csvError}
        </div>
      {/if}
    </div>
  {:else if csvStatus && csvStatus.status !== 'completed'}
    <!-- ─── Live-Card during import ─────────────────────────── -->
    <div
      class="relative overflow-hidden tonus-fadein"
      style="
        background: rgba(20, 20, 24, 0.55);
        backdrop-filter: blur(40px) saturate(1.2);
        -webkit-backdrop-filter: blur(40px) saturate(1.2);
        border: 1px solid var(--color-border-soft);
        border-radius: 22px;
        padding: 32px;
        box-shadow: 0 24px 60px rgba(0, 0, 0, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.05);
      "
    >
      <div class="flex items-center gap-3 mb-3">
        <span
          class="inline-block rounded-full"
          style="width: 8px; height: 8px; background: {accent}; box-shadow: 0 0 12px {accent};"
        ></span>
        <div
          class="font-semibold uppercase"
          style="
            font-size: 11px;
            letter-spacing: 0.24em;
            color: {accent};
          "
        >
          {$t('import.live.eyebrow', { provider: provider || 'provider' })}
        </div>
      </div>

      <div
        class="font-medium mb-4"
        style="
          font-family: var(--font-display);
          font-size: clamp(24px, 6vw, 36px);
          font-weight: 500;
          letter-spacing: -0.025em;
          line-height: 1.05;
          color: var(--color-fg-primary);
        "
      >
        <span class="tabular-nums">{Math.round($tweenProcessed).toLocaleString('de-DE')}</span>
        <span style="color: var(--color-fg-tertiary); font-weight: 300;"> / </span>
        <span class="tabular-nums" style="color: var(--color-fg-secondary);">{csvStatus.total.toLocaleString('de-DE')}</span>
        <span
          style="
            font-size: 14px;
            color: var(--color-fg-tertiary);
            font-weight: 400;
            margin-left: 12px;
            letter-spacing: 0;
          "
        >
          {$t('import.live.tracks_processed')}
        </span>
      </div>

      <ProgressLine
        value={csvProgress}
        pareto={false}
        height={3}
        color={accent}
        glow
      />

      <div class="flex items-center gap-6 mt-4 text-[12px] tabular-nums">
        <div>
          <span style="color: var(--color-fg-tertiary);">{$t('import.live.matched')}</span>
          <span class="ml-1.5 font-medium" style="color: var(--color-status-done);"
            >{Math.round($tweenFound).toLocaleString('de-DE')}</span
          >
        </div>
        {#if (csvStatus.library_match_count ?? 0) > 0}
          <div>
            <span style="color: var(--color-fg-tertiary);">{$t('import.live.library_match')}</span>
            <span class="ml-1.5 font-medium" style="color: var(--color-fg-secondary);"
              >{(csvStatus.library_match_count ?? 0).toLocaleString('de-DE')}</span
            >
          </div>
        {/if}
        <div>
          <span style="color: var(--color-fg-tertiary);">{$t('import.live.not_found')}</span>
          <span class="ml-1.5 font-medium" style="color: var(--color-status-error);"
            >{Math.round($tweenNotFound).toLocaleString('de-DE')}</span
          >
        </div>
        {#if csvStatus.message}
          <div class="ml-auto text-[11px]" style="color: var(--color-fg-tertiary);">
            {csvStatus.message}
          </div>
        {/if}
      </div>
    </div>
  {:else if csvResult}
    <!-- ─── Result Hero ─────────────────────────────────────── -->
    <div
      class="relative overflow-hidden tonus-fadein"
      style="
        background: rgba(20, 20, 24, 0.55);
        backdrop-filter: blur(40px) saturate(1.2);
        -webkit-backdrop-filter: blur(40px) saturate(1.2);
        border: 1px solid var(--color-border-soft);
        border-radius: 22px;
        padding: clamp(18px, 4vw, 32px);
        margin-bottom: 22px;
        box-shadow: 0 24px 60px rgba(0, 0, 0, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.05);
      "
    >
      <div class="flex items-end justify-between flex-wrap gap-4">
        <div>
          <div
            class="font-semibold uppercase"
            style="
              font-size: 11px;
              letter-spacing: 0.24em;
              color: {accent};
              margin-bottom: 14px;
            "
          >
            {$t('import.result.eyebrow')}
          </div>
          <div
            class="font-medium m-0"
            style="
              font-family: var(--font-display);
              font-size: clamp(36px, 8vw, 56px);
              font-weight: 500;
              line-height: 0.95;
              letter-spacing: -0.035em;
              color: var(--color-fg-primary);
            "
          >
            <span class="tabular-nums">{csvResult.found.toLocaleString('de-DE')}</span>
            <span style="font-size: clamp(16px, 3.6vw, 22px); color: var(--color-fg-tertiary); font-weight: 300; letter-spacing: 0;">
              matched
            </span>
          </div>
          <div class="mt-3 text-[14px]" style="color: var(--color-fg-secondary);">
            {#if csvResult.not_found > 0}
              <span style="color: var(--color-status-error); font-weight: 500;"
                >{csvResult.not_found.toLocaleString('de-DE')}</span
              >
              <span style="color: var(--color-fg-tertiary);"> {$t('import.result.not_found_suffix')}</span>
            {:else}
              {$t('import.result.all_found')}
            {/if}
            {#if (csvResult.library_match_count ?? 0) > 0}
              <span style="color: var(--color-fg-tertiary);">{$t('import.result.divider')}</span>
              <span style="color: var(--color-fg-secondary); font-weight: 500;"
                >{(csvResult.library_match_count ?? 0).toLocaleString('de-DE')}</span
              >
              <span style="color: var(--color-fg-tertiary);"> {$t('import.result.library_match_suffix')}</span>
            {/if}
          </div>
        </div>
        <div class="flex items-center gap-3">
          <button
            onclick={resetCsv}
            class="inline-flex items-center gap-1.5 transition-colors"
            style="
              background: rgba(255, 255, 255, 0.04);
              border: 1px solid var(--color-border-soft);
              color: var(--color-fg-secondary);
              padding: 8px 16px;
              border-radius: 999px;
              font-size: 12px;
            "
          >
            {$t('import.result.new_import')}
          </button>
          <button
            onclick={(e) => queueAllMatched(e)}
            disabled={csvQueueAllBusy || csvResult.found === 0}
            class="inline-flex items-center gap-2 transition-opacity disabled:opacity-40"
            style="
              background: {accent};
              color: #1a1410;
              padding: 10px 22px;
              border-radius: 999px;
              font-size: 12px;
              font-weight: 600;
              letter-spacing: 0.04em;
              text-transform: uppercase;
              box-shadow: 0 8px 20px rgba(200, 169, 106, 0.25);
            "
          >
            {#if csvQueueAllBusy}
              <Loader2 size={13} class="animate-spin" />
              Queue …
            {:else}
              <Download size={13} strokeWidth={2} />
              {csvResult.found.toLocaleString('de-DE')} queuen
            {/if}
          </button>
        </div>
      </div>
      {#if csvQueueAllResult}
        <div class="mt-4 text-[12px]" style="color: var(--color-fg-secondary);">
          ✓ {csvQueueAllResult}
        </div>
      {/if}

      {#if csvResult.found + csvResult.not_found > 0}
        {@const matchRate = Math.round(
          (csvResult.found / (csvResult.found + csvResult.not_found)) * 100
        )}
        <div
          class="mt-4 pt-4 flex items-start gap-4 flex-wrap"
          style="border-top: 1px solid var(--color-border-soft);"
        >
          <div class="flex items-center gap-2">
            <span
              class="font-semibold uppercase"
              style="
                font-size: 10px;
                letter-spacing: 0.2em;
                color: var(--color-fg-tertiary);
              "
            >
              {$t('import.match_rate')}
            </span>
            <span
              class="tabular-nums font-medium"
              style="
                font-size: 13px;
                color: {matchRate >= 70 ? 'var(--color-status-done)' : matchRate >= 40 ? accent : 'var(--color-status-error)'};
              "
            >
              {matchRate}%
            </span>
          </div>
          {#if csvStatus?.message}
            <div
              class="text-[11px] tabular-nums flex-1 min-w-0"
              style="
                color: var(--color-fg-tertiary);
                font-family: var(--font-mono);
                line-height: 1.55;
                word-break: break-word;
              "
            >
              {csvStatus.message}
            </div>
          {/if}
        </div>
      {/if}
    </div>

    <!-- ─── Unmatched list ─────────────────────────────────── -->
    {#if csvResult.not_found > 0}
      <div
        class="flex items-center justify-between flex-wrap gap-3"
        style="
          margin-bottom: 14px;
          padding-bottom: 12px;
          border-bottom: 1px solid var(--color-border-soft);
        "
      >
        <div>
          <div
            class="font-semibold uppercase"
            style="
              font-size: 11px;
              letter-spacing: 0.24em;
              color: var(--color-fg-tertiary);
            "
          >
            {$t('import.unmatched.title')}
          </div>
          <div
            class="mt-1 text-[15px]"
            style="color: var(--color-fg-primary); font-weight: 500;"
          >
            <span class="tabular-nums">{csvResult.unmatched.length.toLocaleString('de-DE')}</span>
            <span style="color: var(--color-fg-tertiary); font-weight: 400;">
              von {csvResult.not_found.toLocaleString('de-DE')} angezeigt
            </span>
          </div>
        </div>
        <button
          type="button"
          onclick={exportUnmatched}
          disabled={csvExportBusy}
          class="inline-flex items-center gap-1.5 transition-colors disabled:opacity-50"
          style="
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid var(--color-border-soft);
            color: var(--color-fg-secondary);
            padding: 6px 14px;
            border-radius: 999px;
            font-size: 11.5px;
          "
        >
          {#if csvExportBusy && csvExportProgress}
            <Loader2 size={11} class="animate-spin" />
            CSV {csvExportProgress.loaded.toLocaleString('de-DE')} / {csvExportProgress.total.toLocaleString(
              'de-DE'
            )}
          {:else}
            <Download size={11} strokeWidth={1.8} />
            {$t('import.unmatched.export_csv')}
          {/if}
        </button>
      </div>

      <div class="space-y-1.5">
        {#each csvResult.unmatched as u, i}
          {@const r = recheck[i]}
          <div
            class="px-4 py-2.5 transition-colors"
            style="
              background: rgba(20, 20, 24, 0.4);
              backdrop-filter: blur(20px);
              -webkit-backdrop-filter: blur(20px);
              border: 1px solid var(--color-border-soft);
              border-radius: 12px;
            "
          >
            <div class="flex items-center justify-between gap-3">
              <div class="flex items-center gap-3 min-w-0 flex-1">
                <span
                  class="text-[10.5px] tabular-nums flex-shrink-0"
                  style="color: var(--color-fg-tertiary); font-family: var(--font-mono); width: 36px;"
                >
                  {String(i + 1).padStart(3, '0')}
                </span>
                <div class="text-[13px] truncate" style="color: var(--color-fg-secondary);">
                  <span style="color: var(--color-fg-primary);"
                    >{u.requested_artist || u.original || '?'}</span
                  >
                  {#if u.requested_title}
                    <span style="color: var(--color-fg-tertiary);"> · {u.requested_title}</span>
                  {/if}
                </div>
              </div>
              <button
                type="button"
                onclick={() =>
                  recheckRow(i, u.requested_artist ?? '', u.requested_title ?? '')}
                disabled={r?.loading}
                class="inline-flex items-center gap-1 transition-colors disabled:opacity-50 flex-shrink-0"
                style="
                  background: rgba(255, 255, 255, 0.04);
                  border: 1px solid var(--color-border-soft);
                  color: var(--color-fg-secondary);
                  padding: 4px 10px;
                  border-radius: 999px;
                  font-size: 10.5px;
                "
                aria-label="Bei Deezer {$t('import.recheck.button')}"
              >
                {#if r?.loading}
                  <Loader2 size={10} class="animate-spin" />
                  {$t('import.recheck.checking')}
                {:else if r?.results}
                  <Search size={10} strokeWidth={1.8} />
                  {$t('import.recheck.again')}
                {:else}
                  <Search size={10} strokeWidth={1.8} />
                  nachprüfen
                {/if}
              </button>
            </div>

            {#if r && !r.loading}
              <div
                class="mt-3 pt-3 space-y-1.5"
                style="border-top: 1px solid var(--color-border-soft);"
              >
                {#if r.error}
                  <div class="text-[11px]" style="color: var(--color-status-error);">
                    {r.error}
                  </div>
                {:else if r.results && r.results.length === 0}
                  <div class="text-[11px]" style="color: var(--color-fg-tertiary);">
                    Deezer liefert auch jetzt 0 Treffer — der Track ist tatsächlich nicht im
                    Provider-Katalog (nicht ein Match-Bug).
                  </div>
                {:else if r.results}
                  <div
                    class="text-[10.5px] uppercase mb-1"
                    style="
                      color: {accent};
                      letter-spacing: 0.18em;
                      font-weight: 600;
                    "
                  >
                    Live-Treffer · {r.results.length}
                  </div>
                  {#each r.results as t (t.id)}
                    {@const qs = r.queueState?.[t.id]}
                    <div
                      class="flex items-center gap-3 px-2.5 py-1.5 rounded-md"
                      style="background: rgba(0, 0, 0, 0.25);"
                      data-recheck-track
                    >
                      <div data-cover>
                        <AlbumArt src={t.album_art} alt={t.album} size="sm" />
                      </div>
                      <div class="flex-1 min-w-0">
                        <div
                          class="text-[12px] truncate"
                          style="color: var(--color-fg-primary); font-weight: 500;"
                        >
                          {t.name}
                        </div>
                        <div
                          class="text-[10.5px] truncate"
                          style="color: var(--color-fg-tertiary);"
                        >
                          {t.artist}
                          {#if t.album} · {t.album}{/if}
                        </div>
                      </div>
                      <div
                        class="text-[10px] tabular-nums flex-shrink-0"
                        style="color: var(--color-fg-tertiary); font-family: var(--font-mono);"
                      >
                        {fmtDur(t.duration_ms)}
                      </div>
                      <button
                        type="button"
                        onclick={(e) => queueRecheckTrack(i, t, e)}
                        disabled={qs === 'queued' || qs === 'done' || qs === 'exists'}
                        class="inline-flex items-center gap-1 transition-colors disabled:cursor-default flex-shrink-0"
                        style="
                          background: {qs === 'done'
                          ? 'var(--color-status-done)'
                          : qs === 'exists'
                            ? 'var(--color-surface-3)'
                            : qs === 'error'
                              ? 'var(--color-status-error)'
                              : accentSoft};
                          color: {qs === 'exists' ? 'var(--color-fg-secondary)' : '#1a1410'};
                          padding: 3px 9px;
                          border-radius: 999px;
                          font-size: 10px;
                          font-weight: 600;
                          letter-spacing: 0.04em;
                          text-transform: uppercase;
                        "
                      >
                        {#if qs === 'queued'}
                          <Loader2 size={10} class="animate-spin" />
                        {:else if qs === 'done'}
                          ✓ queued
                        {:else if qs === 'exists'}
                          ✓ vorhanden
                        {:else if qs === 'error'}
                          fehler
                        {:else}
                          <Download size={10} strokeWidth={2} />
                          queuen
                        {/if}
                      </button>
                    </div>
                  {/each}
                {/if}
              </div>
            {/if}
          </div>
        {/each}
      </div>

      {#if csvResult.unmatched.length < csvResult.not_found}
        <button
          onclick={loadMoreUnmatched}
          disabled={csvLoadMoreBusy}
          class="w-full inline-flex items-center justify-center gap-2 transition-colors disabled:opacity-50"
          style="
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid var(--color-border-soft);
            color: var(--color-fg-secondary);
            padding: 10px 14px;
            border-radius: 12px;
            font-size: 12px;
            margin-top: 14px;
          "
        >
          {#if csvLoadMoreBusy}
            <Loader2 size={13} class="animate-spin" />
            lade …
          {:else}
            Mehr laden
            <span class="text-[11px]" style="color: var(--color-fg-tertiary);">
              · noch {(csvResult.not_found - csvResult.unmatched.length).toLocaleString('de-DE')}
            </span>
          {/if}
        </button>
      {/if}
    {/if}
  {/if}
</section>

<style>
  /* Mobile-Pass: Hero stacked, Vinyl shrinkt via reactive size-prop. */
  @media (max-width: 640px) {
    .tonus-import-hero {
      grid-template-columns: 1fr !important;
      gap: 18px !important;
      justify-items: start;
    }
    .tonus-import-vinyl {
      justify-content: flex-start !important;
    }
  }
</style>

