// Package tasks defines the asynchronous job format for the plugin's
// TaskQueue. The cron-callback enqueues one job per Navidrome user per
// run; the OnTaskExecute hook deserializes and dispatches them.
//
// Two job types:
//   - JobTriggerSync   → POST /api/plugin/sync for one user (fire-and-forget)
//   - JobReconcile     → query /api/plugin/finished-tracks + push to Subsonic
//
// They run in the same TaskQueue (concurrency 1) so per-user runs never
// overlap and the playlist-update is idempotent regardless of order.
package tasks

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/navidrome/navidrome/plugins/pdk/go/host"
	"github.com/navidrome/navidrome/plugins/pdk/go/pdk"

	"tonus-navidrome-plugin/internal/client"
	"tonus-navidrome-plugin/internal/subsonic"
)

// QueueName must be created in OnInit via host.TaskCreateQueue and re-used
// by both Enqueue and the queue-config in main.go.
const QueueName = "tonus-sync"

type JobType string

const (
	JobTriggerSync JobType = "trigger-sync"
	JobReconcile   JobType = "reconcile"
)

// Job is the canonical payload format. Marshal → bytes → host.TaskEnqueue
// in main.go; OnTaskExecute does Unmarshal → Dispatch.
type Job struct {
	Type             JobType `json:"type"`
	NavidromeUser    string  `json:"navidrome_user"`
	ListenBrainzUser string  `json:"listenbrainz_user"`
	PlaylistName     string  `json:"playlist_name"` // already date-substituted at enqueue time

	// Connection (passed in so each task is fully self-contained).
	TonusURL   string `json:"tonus_url"`
	TonusToken string `json:"tonus_token"`

	// Discovery tuning.
	TopArtists      int `json:"top_artists"`
	TracksPerArtist int `json:"tracks_per_artist"`
	MaxQueuePerRun  int `json:"max_queue_per_run"`
}

// Marshal returns the task payload bytes for host.TaskEnqueue.
func (j *Job) Marshal() ([]byte, error) {
	return json.Marshal(j)
}

// Dispatch routes the job to its handler. Return value matches the
// taskworker.OnTaskExecute contract: (unrecoverableErr string, retryErr error).
//   - non-empty string → unrecoverable, log + drop
//   - non-nil error    → host should retry (per QueueConfig.MaxRetries)
//   - both empty       → success
func (j *Job) Dispatch() (string, error) {
	switch j.Type {
	case JobTriggerSync:
		return runTriggerSync(j)
	case JobReconcile:
		return runReconcile(j)
	default:
		return fmt.Sprintf("unknown job type %q", j.Type), nil
	}
}

// ---- TriggerSync ---------------------------------------------------------

func runTriggerSync(j *Job) (string, error) {
	cl := client.New(j.TonusURL, j.TonusToken)

	// Health-Check first so config errors surface immediately, not 24h later.
	h, err := cl.Health()
	if err != nil {
		return "", fmt.Errorf("trigger-sync %q: health: %w", j.NavidromeUser, err)
	}
	if h.AuthRequired && j.TonusToken == "" {
		return fmt.Sprintf("trigger-sync %q: backend requires token but none configured", j.NavidromeUser), nil
	}

	resp, err := cl.TriggerSync(client.TriggerSyncRequest{
		ListenBrainzUser: j.ListenBrainzUser,
		TopArtists:       j.TopArtists,
		TracksPerArtist:  j.TracksPerArtist,
		MaxTotal:         j.MaxQueuePerRun,
		Location:         "navidrome",
		PlaylistName:     j.PlaylistName,
		NavidromeUser:    j.NavidromeUser,
	})
	if err != nil {
		return "", fmt.Errorf("trigger-sync %q: %w", j.NavidromeUser, err)
	}

	pdk.Log(pdk.LogInfo, fmt.Sprintf(
		"trigger-sync %q (lb=%s) accepted: started=%v %s",
		j.NavidromeUser, j.ListenBrainzUser, resp.Started, resp.Message,
	))
	writeUserStatus(j.NavidromeUser, userStatus{
		LastTriggerMs:    time.Now().UnixMilli(),
		LastTriggerOK:    true,
		LastTriggerInfo:  fmt.Sprintf("backend ack: %s", resp.Message),
		ListenBrainzUser: j.ListenBrainzUser,
		PlaylistName:     j.PlaylistName,
	})
	return "", nil
}

// ---- Reconcile -----------------------------------------------------------

func runReconcile(j *Job) (string, error) {
	if j.PlaylistName == "" {
		// Without a playlist target there's nothing to reconcile.
		return "", nil
	}
	cl := client.New(j.TonusURL, j.TonusToken)

	// 1. Pull a small batch of finished tracks (limit=25 keeps backend
	// search3-resolution + plugin Subsonic-write under the 30s task timeout).
	// Remaining tracks come along on subsequent cron triggers.
	finished, err := cl.FinishedTracks(j.NavidromeUser, 60, j.PlaylistName)
	if err != nil {
		return "", fmt.Errorf("reconcile %q: finished-tracks: %w", j.NavidromeUser, err)
	}
	if finished.Count == 0 {
		pdk.Log(pdk.LogDebug, fmt.Sprintf("reconcile %q: no finished tracks yet", j.NavidromeUser))
		return "", nil
	}

	// 2. Subsonic client bound to this user — host injects auth.
	sub := subsonic.New(j.NavidromeUser)

	// 3. Find or create the playlist.
	pl, err := sub.FindPlaylistByName(j.PlaylistName)
	if err != nil {
		return "", fmt.Errorf("reconcile %q: find playlist: %w", j.NavidromeUser, err)
	}
	if pl == nil {
		pl, err = sub.CreatePlaylist(j.PlaylistName, nil)
		if err != nil {
			return "", fmt.Errorf("reconcile %q: create playlist: %w", j.NavidromeUser, err)
		}
		pdk.Log(pdk.LogInfo, fmt.Sprintf(
			"reconcile %q: created playlist %q (id=%s)", j.NavidromeUser, pl.Name, pl.ID,
		))
	}

	// 4. Backend already resolved Subsonic-IDs serverside (admin-auth search3,
	// cached in payload). For the rare case where backend couldn't resolve
	// (Subsonic indexer not yet caught up at queue-time) we fall back to a
	// single search3 call here. Misses are retried on the next cron tick.
	songIDs := make([]string, 0, finished.Count)
	unresolved := 0
	for _, ft := range finished.Items {
		sid := ft.SubsonicTrackID
		if sid == "" {
			s, err := sub.SearchTrackID(ft.Artist, ft.Title)
			if err != nil {
				pdk.Log(pdk.LogWarn, fmt.Sprintf(
					"reconcile %q: search3 failed for %s/%s: %s",
					j.NavidromeUser, ft.Artist, ft.Title, err.Error(),
				))
				continue
			}
			sid = s
		}
		if sid == "" {
			unresolved++
			continue
		}
		songIDs = append(songIDs, sid)
	}

	added, err := sub.AddTracksIdempotent(pl, songIDs)
	if err != nil {
		return "", fmt.Errorf("reconcile %q: add tracks: %w", j.NavidromeUser, err)
	}

	pdk.Log(pdk.LogInfo, fmt.Sprintf(
		"reconcile %q: playlist=%q +%d tracks (already in playlist: %d, unresolved: %d, total finished: %d)",
		j.NavidromeUser, pl.Name, added, len(songIDs)-added, unresolved, finished.Count,
	))
	writeUserStatus(j.NavidromeUser, userStatus{
		LastReconcileMs:        time.Now().UnixMilli(),
		LastReconcileAdded:     added,
		LastReconcileUnresolved: unresolved,
		LastReconcileTotal:     finished.Count,
		ListenBrainzUser:       j.ListenBrainzUser,
		PlaylistName:           j.PlaylistName,
		PlaylistID:             pl.ID,
	})
	return "", nil
}

// ---- KVStore-Status (per user) -------------------------------------------

type userStatus struct {
	LastTriggerMs           int64  `json:"last_trigger_ms,omitempty"`
	LastTriggerOK           bool   `json:"last_trigger_ok,omitempty"`
	LastTriggerInfo         string `json:"last_trigger_info,omitempty"`
	LastReconcileMs         int64  `json:"last_reconcile_ms,omitempty"`
	LastReconcileAdded      int    `json:"last_reconcile_added,omitempty"`
	LastReconcileUnresolved int    `json:"last_reconcile_unresolved,omitempty"`
	LastReconcileTotal      int    `json:"last_reconcile_total,omitempty"`
	ListenBrainzUser        string `json:"listenbrainz_user,omitempty"`
	PlaylistName            string `json:"playlist_name,omitempty"`
	PlaylistID              string `json:"playlist_id,omitempty"`
}

// writeUserStatus stores per-user status snapshots. Key = "status:<user>",
// merged on each write so trigger + reconcile fields don't overwrite each
// other.
func writeUserStatus(user string, partial userStatus) {
	if user == "" {
		return
	}
	key := "status:" + strings.TrimSpace(user)

	// Merge with existing.
	var current userStatus
	if existing, ok, err := host.KVStoreGet(key); err == nil && ok {
		_ = json.Unmarshal(existing, &current)
	}
	if partial.LastTriggerMs > 0 {
		current.LastTriggerMs = partial.LastTriggerMs
		current.LastTriggerOK = partial.LastTriggerOK
		current.LastTriggerInfo = partial.LastTriggerInfo
	}
	if partial.LastReconcileMs > 0 {
		current.LastReconcileMs = partial.LastReconcileMs
		current.LastReconcileAdded = partial.LastReconcileAdded
		current.LastReconcileUnresolved = partial.LastReconcileUnresolved
		current.LastReconcileTotal = partial.LastReconcileTotal
	}
	if partial.ListenBrainzUser != "" {
		current.ListenBrainzUser = partial.ListenBrainzUser
	}
	if partial.PlaylistName != "" {
		current.PlaylistName = partial.PlaylistName
	}
	if partial.PlaylistID != "" {
		current.PlaylistID = partial.PlaylistID
	}

	blob, err := json.Marshal(current)
	if err != nil {
		pdk.Log(pdk.LogWarn, "marshal user-status: "+err.Error())
		return
	}
	if err := host.KVStoreSet(key, blob); err != nil {
		pdk.Log(pdk.LogWarn, "KVStoreSet "+key+": "+err.Error())
	}
}
