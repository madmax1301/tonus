/**
 * Master-Translation-Map für Tonus.
 *
 * Konvention: Keys sind dot-separated, gruppiert nach Screen/Komponente.
 * Beide Sprachen müssen denselben Key-Set haben — fehlt ein Key in EN,
 * fällt der t()-Helper auf DE zurück (siehe i18n.ts).
 *
 * Platzhalter im Format `{name}` werden via t-Helper ersetzt.
 */

export type Lang = 'de' | 'en';

export const strings = {
  de: {
    // ── Common ──────────────────────────────────────────
    'common.save': 'speichern',
    'common.saved': 'gespeichert',
    'common.cancel': 'Abbrechen',
    'common.delete': 'Löschen',
    'common.error': 'Fehler',
    'common.loading': 'lade …',
    'common.queueing': 'queue …',
    'common.in_queue': '✓ in Queue',
    'common.exists': '✓ vorhanden',
    'common.queued': '✓ queued',
    'common.queue': 'queuen',
    'common.download': 'Download',
    'common.matches': 'Treffer',
    'common.results': 'Treffer',
    'common.no_results': 'Keine Treffer.',

    // ── Topbar / Navigation ────────────────────────────
    'nav.library': 'Bibliothek',
    'nav.queue': 'Warteschlange',
    'nav.import': 'Import',
    'nav.settings': 'Einstellungen',
    'nav.token_active': 'Token aktiv',
    'nav.token_inactive': 'Token nicht gesetzt',

    // ── Library ─────────────────────────────────────────
    'library.eyebrow': 'Bibliothek',
    'library.title.before': 'Was hörst du',
    'library.title.italic': 'heute Abend',
    'library.title.after': '?',
    'library.description':
      'Suche über Deezer, Spotify und YouTube — Tonus matcht Tags und legt den Track sauber in deine Bibliothek.',
    'library.mode.tracks': 'Tracks',
    'library.mode.albums': 'Alben',
    'library.mode.url': 'URL',
    'library.mode.youtube_match': 'YouTube-Match',
    'library.placeholder.tracks': 'Track, Artist oder Album …',
    'library.placeholder.albums': 'Album oder Artist …',
    'library.placeholder.url': 'https:// — YouTube, SoundCloud, Bandcamp, Vimeo …',
    'library.placeholder.youtube': 'https://www.youtube.com/watch?v=…',
    'library.url.queue_button': 'In Queue',
    'library.youtube.search_button': 'Match suchen',
    'library.youtube.direct_button': 'Direkt laden ohne Match',
    'library.url.hint':
      'Lädt direkt via yt-dlp ohne Metadata-Match. Title und Artist-Tag bleiben so wie auf der Quelle. Brauchst du saubere Tags, nutze stattdessen YouTube-Match.',
    'library.youtube.hint':
      'Sucht den Track aus dem YouTube-Video im gewählten Provider (Deezer/Spotify) und lädt ihn von dort mit sauberen Tags + Cover. Bei keinem Match-Treffer kannst du immer noch direkt laden.',
    'library.youtube.label': 'YouTube:',
    'library.youtube.candidates_hint': '{count} mögliche Treffer · wähle einen für Tags + Cover',
    'library.button.this_match': 'Diesen Match',
    'library.empty.no_query':
      'Tipp: Suchbegriff eingeben und ↵ drücken, oder einfach 320 ms tippen.',
    'library.provider.default_suffix': 'Default',
    'empty.library.eyebrow': 'Kein Album bisher',
    'empty.library.title': 'Deine Bibliothek\nwartet auf den ersten Track.',
    'empty.library.body':
      'Suche einen Song, importiere eine CSV mit gelikten Tracks oder lade direkt eine YouTube-URL — Tonus übernimmt den Rest.',
    'empty.library.cta_search': 'Track suchen',
    'empty.library.cta_csv': 'CSV importieren',
    'empty.library.tip':
      'Tonus indexiert Tracks bei Bedarf — kein Hintergrund-Sync, keine Quota.',
    'empty.queue.eyebrow': 'Warteschlange leer',
    'empty.queue.title': 'Nichts läuft.\nNichts wartet.',
    'empty.queue.body':
      'Pack einen Track in die Queue — Tonus matcht die beste verfügbare Quelle und legt ihn in deine Bibliothek.',
    'empty.queue.cta_search': 'Track suchen',
    'empty.queue.cta_library': 'Zur Bibliothek',
    'empty.queue.tip':
      'Du erkennst aktive Jobs am Queue-Vinyl unten rechts — die Zahl zählt nur live + queued + Fehler.',

    // ── Album-Detail ────────────────────────────────────
    'album.back': 'Zurück',
    'album.tracks_count': '{count} Tracks',
    'album.duration': 'Gesamt {duration}',
    'album.queue_all': 'Alben-Download',
    'album.queue_track': 'queuen',
    'album.button.queueing': 'wird gequeued …',
    'album.button.exists': 'vorhanden',
    'album.button.error': 'Fehler — erneut versuchen',
    'album.button.full_album': 'Komplettes Album · {count} Tracks',
    'album.open_source': 'Bei Quelle öffnen',
    'album.tracks_table.heading': 'Tracks',
    'album.tracks_table.col_no': 'Nr',
    'album.tracks_table.col_title': 'Titel',
    'album.tracks_table.col_duration': 'Dauer',
    'album.row.in_library': 'in Library',
    'album.row.track': 'Track',
    'album.error.load_failed': 'Album konnte nicht geladen werden',

    // ── Queue ───────────────────────────────────────────
    'queue.eyebrow.live': 'Live',
    'queue.eyebrow.waiting': 'Wartet',
    'queue.eyebrow.ready': 'Bereit',
    'queue.eyebrow.idle': 'Ruhig',
    'queue.featured.lane_in': 'Lane in {time}',
    'queue.title': 'Warteschlange',
    'queue.total_jobs': '{count} Jobs gesamt',
    'queue.shown': '{count} sichtbar',
    'queue.filter.all': 'Alle',
    'queue.filter.queued': 'Queued',
    'queue.filter.processing': 'Aktiv',
    'queue.filter.error': 'Fehler',
    'queue.filter.completed': 'Fertig',
    'queue.filter.search_placeholder': 'Filtern …',
    'queue.bulk.retry_errors': 'Fehler retry',
    'queue.bulk.cleanup': 'Aufräumen',
    'queue.bulk.clear_all': 'Leeren',
    'queue.empty': 'Warteschlange ist leer.',
    'queue.row.queued': 'queued',
    'queue.row.processing': 'läuft',
    'queue.row.completed': 'fertig',
    'queue.row.error': 'Fehler',
    'queue.row.retry': 'erneut',
    'queue.row.delete': 'entfernen',
    'queue.origin.plugin': 'Plugin',
    'queue.origin.url': 'URL',
    'queue.origin.album': 'Album',
    'queue.origin.search': 'Suche',
    'queue.dest.playlist': 'Playlist',
    'queue.dest.navidrome': 'Navidrome',
    'queue.dest.local': 'Local',

    // ── Import ──────────────────────────────────────────
    'import.eyebrow': 'Bulk Import',
    'import.title.before': 'Hunderte Tracks.',
    'import.title.italic': 'Eine Liste.',
    'import.description':
      'CSV oder Freitext rein — Tonus matcht jede Zeile gegen Deezer/Spotify und queued sauber, was zu finden war. Was nicht passt, kannst du als CSV exportieren.',
    'import.dropzone.eyebrow': 'Eingabe',
    'import.dropzone.title': 'Datei droppen oder Liste einfügen',
    'import.dropzone.choose_file': 'CSV-Datei wählen',
    'import.dropzone.clear': 'Leeren',
    'import.dropzone.drop_overlay': 'Drop to import',
    'import.dropzone.placeholder':
      'Künstler;Titel\nDaft Punk;Get Lucky\nQueen;Bohemian Rhapsody\n\noder eine Zeile pro Track als Freitext',
    'import.dropzone.lines_ready': '{count} Zeilen bereit',
    'import.dropzone.line_ready': '{count} Zeile bereit',
    'import.dropzone.format_hint': 'Liste einfügen oder Datei droppen — Format:',
    'import.start': 'Import starten',
    'import.live.eyebrow': 'Live · matching against {provider}',
    'import.live.tracks_processed': 'Tracks verarbeitet',
    'import.live.matched': 'matched',
    'import.live.not_found': 'nicht gefunden',
    'import.result.eyebrow': 'Import abgeschlossen',
    'import.result.matched_label': 'matched',
    'import.result.not_found_count': '{count} nicht gefunden',
    'import.result.all_found': 'Alles gefunden — saubere Liste',
    'import.result.queue_n': '{count} queuen',
    'import.result.new_import': 'Neuer Import',
    'import.match_rate': 'Match-Rate',
    'import.unmatched.title': 'Nicht gefunden',
    'import.unmatched.shown': '{shown} von {total} angezeigt',
    'import.unmatched.export_csv': 'Als CSV exportieren',
    'import.unmatched.exporting': 'CSV {loaded} / {total}',
    'import.unmatched.load_more': 'Mehr laden',
    'import.unmatched.remaining': 'noch {count}',
    'import.recheck.button': 'nachprüfen',
    'import.recheck.again': 'erneut',
    'import.recheck.checking': 'prüfe …',
    'import.recheck.live_hits': 'Live-Treffer · {count}',
    'import.recheck.no_hits':
      'Deezer liefert auch jetzt 0 Treffer — der Track ist tatsächlich nicht im Provider-Katalog.',
    'import.queueing': 'Queue …',

    // ── Settings ────────────────────────────────────────
    'settings.eyebrow': 'Setup',
    'settings.title.before': 'Deine',
    'settings.title.italic': 'Konfiguration',
    'settings.title.after': '.',
    'settings.description.prefix': 'Browser-Defaults und Backend-Übersicht. Lokale Einstellungen leben im localStorage, Backend-Konfiguration kommt aus',
    'settings.description.env': 'backend/.env',
    'settings.description.suffix': 'und ist read-only sichtbar.',

    'settings.section.auth': 'Authentifizierung',
    'settings.section.defaults': 'Standard-Verhalten',
    'settings.section.backend': 'Backend-Info',
    'settings.section.local': 'Lokale Daten',
    'settings.section.language': 'Sprache',

    // Auth section
    'settings.auth.eyebrow': 'API-Token',
    'settings.auth.title': 'Browser ↔ Backend',
    'settings.auth.description.prefix': 'Muss exakt mit',
    'settings.auth.description.env_var': 'TONUS_API_TOKEN',
    'settings.auth.description.middle': 'in',
    'settings.auth.description.env_file': 'backend/.env',
    'settings.auth.description.suffix':
      'übereinstimmen. Bleibt nur in deinem Browser, wird nicht ans Backend zurückgespiegelt.',

    // Defaults section
    'settings.defaults.provider.eyebrow': 'Standard-Provider',
    'settings.defaults.provider.title': 'Suche & Reverse-Lookup',
    'settings.defaults.provider.active': 'aktiv:',
    'settings.defaults.provider.backend_default': 'Backend-Default',

    'settings.defaults.location.eyebrow': 'Download-Ziel',
    'settings.defaults.location.title': 'Wo Tracks landen',
    'settings.defaults.location.navidrome': 'Navidrome (in Bibliothek)',
    'settings.defaults.location.local': 'Local (downloads/)',

    'settings.defaults.audio.eyebrow': 'Audio-Codec',
    'settings.defaults.audio.title': 'Format & Bitrate',
    'settings.defaults.audio.format_label': 'Format',
    'settings.defaults.audio.bitrate_label': 'Bitrate',
    'settings.defaults.audio.note':
      'FLAC ignoriert die Bitrate (lossless). Opus & OGG mappen auf andere Skalen — der Backend-Wert wird passend übersetzt.',

    // Backend-Info section
    'settings.backend.eyebrow': 'Read-Only · backend/.env',
    'settings.backend.title': 'Was das Backend gerade nutzt',
    'settings.backend.field.default_provider': 'Default-Provider',
    'settings.backend.field.configured_providers': 'Konfigurierte Provider',
    'settings.backend.field.missing_providers': 'fehlt:',
    'settings.backend.field.default_format': 'Default-Format',
    'settings.backend.field.available_formats': 'Verfügbare Formate',
    'settings.backend.field.navidrome_path': 'Navidrome-Pfad',
    'settings.backend.libraries.title': 'Navidrome-Bibliotheken · {count}',

    // Local section
    'settings.local.eyebrow': 'Browser-Storage',
    'settings.local.title': 'Reset auf Werkseinstellungen',
    'settings.local.description.prefix': 'Löscht alle',
    'settings.local.description.key': 'tonus_*',
    'settings.local.description.suffix':
      '-Schlüssel im Browser-localStorage — Token, Defaults, Queue-Snapshot. Anschließend lädt die Seite neu und alle Defaults springen auf die Backend-Werte zurück.',
    'settings.local.confirm':
      'Alle lokalen Tonus-Einstellungen löschen? Token, Provider, Location, etc.',
    'settings.local.button': 'Lokale Daten löschen',
    'settings.local.cleared': 'geleert · lade neu',

    // Language section
    'settings.language.eyebrow': 'Display',
    'settings.language.title': 'Sprache der Oberfläche',
    'settings.language.description':
      'Ändert die UI-Sprache im Browser. Backend-Antworten (Track-Titel, Artist-Namen) bleiben unverändert — die kommen direkt vom Provider.',
    'settings.language.de': 'Deutsch',
    'settings.language.en': 'English'
  },
  en: {
    // ── Common ──────────────────────────────────────────
    'common.save': 'save',
    'common.saved': 'saved',
    'common.cancel': 'Cancel',
    'common.delete': 'Delete',
    'common.error': 'Error',
    'common.loading': 'loading …',
    'common.queueing': 'queuing …',
    'common.in_queue': '✓ queued',
    'common.exists': '✓ exists',
    'common.queued': '✓ queued',
    'common.queue': 'queue',
    'common.download': 'Download',
    'common.matches': 'matches',
    'common.results': 'results',
    'common.no_results': 'No results.',

    // ── Topbar / Navigation ────────────────────────────
    'nav.library': 'Library',
    'nav.queue': 'Queue',
    'nav.import': 'Import',
    'nav.settings': 'Settings',
    'nav.token_active': 'Token active',
    'nav.token_inactive': 'Token not set',

    // ── Library ─────────────────────────────────────────
    'library.eyebrow': 'Library',
    'library.title.before': 'What are you listening to',
    'library.title.italic': 'tonight',
    'library.title.after': '?',
    'library.description':
      'Search across Deezer, Spotify and YouTube — Tonus matches tags and drops the track cleanly into your library.',
    'library.mode.tracks': 'Tracks',
    'library.mode.albums': 'Albums',
    'library.mode.url': 'URL',
    'library.mode.youtube_match': 'YouTube Match',
    'library.placeholder.tracks': 'Track, artist or album …',
    'library.placeholder.albums': 'Album or artist …',
    'library.placeholder.url': 'https:// — YouTube, SoundCloud, Bandcamp, Vimeo …',
    'library.placeholder.youtube': 'https://www.youtube.com/watch?v=…',
    'library.url.queue_button': 'Queue',
    'library.youtube.search_button': 'Find match',
    'library.youtube.direct_button': 'Download without match',
    'library.url.hint':
      'Downloads directly via yt-dlp without metadata matching. Title and artist tag stay as on the source. For clean tags use YouTube Match instead.',
    'library.youtube.hint':
      'Searches the track from the YouTube video in the chosen provider (Deezer/Spotify) and downloads it from there with clean tags + cover. If no match is found you can still download directly.',
    'library.youtube.label': 'YouTube:',
    'library.youtube.candidates_hint': '{count} possible matches · pick one for tags + cover',
    'library.button.this_match': 'This match',
    'library.empty.no_query':
      'Tip: type a query and press ↵, or just type for 320 ms.',
    'library.provider.default_suffix': 'Default',
    'empty.library.eyebrow': 'No album yet',
    'empty.library.title': 'Your library\nis waiting for the first track.',
    'empty.library.body':
      'Search a song, import a CSV with liked tracks or drop in a YouTube URL — Tonus does the rest.',
    'empty.library.cta_search': 'Search tracks',
    'empty.library.cta_csv': 'Import CSV',
    'empty.library.tip': 'Tonus indexes on-demand — no background sync, no quota.',
    'empty.queue.eyebrow': 'Queue is empty',
    'empty.queue.title': 'Nothing running.\nNothing waiting.',
    'empty.queue.body':
      'Drop a track into the queue — Tonus picks the best source available and lands it in your library.',
    'empty.queue.cta_search': 'Search tracks',
    'empty.queue.cta_library': 'Open library',
    'empty.queue.tip':
      'Active jobs show up on the queue vinyl bottom-right — the counter shows live + queued + errored only.',

    // ── Album-Detail ────────────────────────────────────
    'album.back': 'Back',
    'album.tracks_count': '{count} tracks',
    'album.duration': 'Total {duration}',
    'album.queue_all': 'Album download',
    'album.queue_track': 'queue',
    'album.button.queueing': 'queuing …',
    'album.button.exists': 'exists',
    'album.button.error': 'Error — retry',
    'album.button.full_album': 'Full album · {count} tracks',
    'album.open_source': 'Open at source',
    'album.tracks_table.heading': 'Tracks',
    'album.tracks_table.col_no': 'No',
    'album.tracks_table.col_title': 'Title',
    'album.tracks_table.col_duration': 'Duration',
    'album.row.in_library': 'in library',
    'album.row.track': 'Track',
    'album.error.load_failed': 'Could not load album',

    // ── Queue ───────────────────────────────────────────
    'queue.eyebrow.live': 'Live',
    'queue.eyebrow.waiting': 'Waiting',
    'queue.eyebrow.ready': 'Ready',
    'queue.eyebrow.idle': 'Idle',
    'queue.featured.lane_in': 'Lane in {time}',
    'queue.title': 'Queue',
    'queue.total_jobs': '{count} jobs total',
    'queue.shown': '{count} shown',
    'queue.filter.all': 'All',
    'queue.filter.queued': 'Queued',
    'queue.filter.processing': 'Active',
    'queue.filter.error': 'Errors',
    'queue.filter.completed': 'Done',
    'queue.filter.search_placeholder': 'Filter …',
    'queue.bulk.retry_errors': 'Retry errors',
    'queue.bulk.cleanup': 'Cleanup',
    'queue.bulk.clear_all': 'Clear all',
    'queue.empty': 'Queue is empty.',
    'queue.row.queued': 'queued',
    'queue.row.processing': 'running',
    'queue.row.completed': 'done',
    'queue.row.error': 'Error',
    'queue.row.retry': 'retry',
    'queue.row.delete': 'remove',
    'queue.origin.plugin': 'Plugin',
    'queue.origin.url': 'URL',
    'queue.origin.album': 'Album',
    'queue.origin.search': 'Search',
    'queue.dest.playlist': 'Playlist',
    'queue.dest.navidrome': 'Navidrome',
    'queue.dest.local': 'Local',

    // ── Import ──────────────────────────────────────────
    'import.eyebrow': 'Bulk Import',
    'import.title.before': 'Hundreds of tracks.',
    'import.title.italic': 'One list.',
    'import.description':
      'CSV or freetext in — Tonus matches every line against Deezer/Spotify and queues cleanly what could be found. What didn’t fit you can export as CSV.',
    'import.dropzone.eyebrow': 'Input',
    'import.dropzone.title': 'Drop a file or paste a list',
    'import.dropzone.choose_file': 'Choose CSV file',
    'import.dropzone.clear': 'Clear',
    'import.dropzone.drop_overlay': 'Drop to import',
    'import.dropzone.placeholder':
      'Artist;Title\nDaft Punk;Get Lucky\nQueen;Bohemian Rhapsody\n\nor one line per track as freetext',
    'import.dropzone.lines_ready': '{count} lines ready',
    'import.dropzone.line_ready': '{count} line ready',
    'import.dropzone.format_hint': 'Paste a list or drop a file — format:',
    'import.start': 'Start import',
    'import.live.eyebrow': 'Live · matching against {provider}',
    'import.live.tracks_processed': 'tracks processed',
    'import.live.matched': 'matched',
    'import.live.not_found': 'not found',
    'import.result.eyebrow': 'Import complete',
    'import.result.matched_label': 'matched',
    'import.result.not_found_count': '{count} not found',
    'import.result.all_found': 'All found — clean list',
    'import.result.queue_n': 'queue {count}',
    'import.result.new_import': 'New import',
    'import.match_rate': 'Match rate',
    'import.unmatched.title': 'Not found',
    'import.unmatched.shown': '{shown} of {total} shown',
    'import.unmatched.export_csv': 'Export as CSV',
    'import.unmatched.exporting': 'CSV {loaded} / {total}',
    'import.unmatched.load_more': 'Load more',
    'import.unmatched.remaining': '{count} remaining',
    'import.recheck.button': 'recheck',
    'import.recheck.again': 'again',
    'import.recheck.checking': 'checking …',
    'import.recheck.live_hits': 'Live hits · {count}',
    'import.recheck.no_hits':
      'Deezer also returns 0 results now — the track is genuinely not in the provider catalog.',
    'import.queueing': 'queue …',

    // ── Settings ────────────────────────────────────────
    'settings.eyebrow': 'Setup',
    'settings.title.before': 'Your',
    'settings.title.italic': 'configuration',
    'settings.title.after': '.',
    'settings.description.prefix':
      'Browser defaults and backend overview. Local preferences live in localStorage, backend configuration comes from',
    'settings.description.env': 'backend/.env',
    'settings.description.suffix': 'and is read-only here.',

    'settings.section.auth': 'Authentication',
    'settings.section.defaults': 'Defaults',
    'settings.section.backend': 'Backend info',
    'settings.section.local': 'Local data',
    'settings.section.language': 'Language',

    // Auth section
    'settings.auth.eyebrow': 'API token',
    'settings.auth.title': 'Browser ↔ Backend',
    'settings.auth.description.prefix': 'Must match exactly',
    'settings.auth.description.env_var': 'TONUS_API_TOKEN',
    'settings.auth.description.middle': 'in',
    'settings.auth.description.env_file': 'backend/.env',
    'settings.auth.description.suffix':
      '. Stays in your browser only, is never sent back to the backend.',

    // Defaults section
    'settings.defaults.provider.eyebrow': 'Default provider',
    'settings.defaults.provider.title': 'Search & reverse lookup',
    'settings.defaults.provider.active': 'active:',
    'settings.defaults.provider.backend_default': 'Backend default',

    'settings.defaults.location.eyebrow': 'Download target',
    'settings.defaults.location.title': 'Where tracks land',
    'settings.defaults.location.navidrome': 'Navidrome (in library)',
    'settings.defaults.location.local': 'Local (downloads/)',

    'settings.defaults.audio.eyebrow': 'Audio codec',
    'settings.defaults.audio.title': 'Format & bitrate',
    'settings.defaults.audio.format_label': 'Format',
    'settings.defaults.audio.bitrate_label': 'Bitrate',
    'settings.defaults.audio.note':
      'FLAC ignores bitrate (lossless). Opus & OGG map to different scales — the backend value is translated accordingly.',

    // Backend-Info section
    'settings.backend.eyebrow': 'Read-only · backend/.env',
    'settings.backend.title': 'What the backend currently uses',
    'settings.backend.field.default_provider': 'Default provider',
    'settings.backend.field.configured_providers': 'Configured providers',
    'settings.backend.field.missing_providers': 'missing:',
    'settings.backend.field.default_format': 'Default format',
    'settings.backend.field.available_formats': 'Available formats',
    'settings.backend.field.navidrome_path': 'Navidrome path',
    'settings.backend.libraries.title': 'Navidrome libraries · {count}',

    // Local section
    'settings.local.eyebrow': 'Browser storage',
    'settings.local.title': 'Reset to factory defaults',
    'settings.local.description.prefix': 'Removes all',
    'settings.local.description.key': 'tonus_*',
    'settings.local.description.suffix':
      ' keys from the browser localStorage — token, defaults, queue snapshot. The page reloads afterwards and all defaults snap back to the backend values.',
    'settings.local.confirm':
      'Delete all local Tonus settings? Token, provider, location, etc.',
    'settings.local.button': 'Delete local data',
    'settings.local.cleared': 'cleared · reloading',

    // Language section
    'settings.language.eyebrow': 'Display',
    'settings.language.title': 'Interface language',
    'settings.language.description':
      'Changes the UI language in your browser. Backend responses (track titles, artist names) stay unchanged — those come straight from the provider.',
    'settings.language.de': 'Deutsch',
    'settings.language.en': 'English'
  }
} as const;

export type StringKey = keyof (typeof strings)['de'];
