// tonus-navidrome-plugin: Navidrome plugin that triggers the tonus
// FastAPI backend for per-user discovery + automated playlist creation.
//
// Architecture:
//   1. OnInit registers the cron and creates the TaskQueue. No work yet.
//   2. OnCallback (every cron tick) reads the config (tonus URL/token,
//      schedule, global discovery tuning, the user-mapping array) and enqueues
//      one trigger-sync task + one reconcile task per configured user. Returns
//      in <1s — well below the plugin-callback timeout.
//   3. OnTaskExecute runs each task asynchronously. trigger-sync hits
//      /api/plugin/sync. reconcile pulls /api/plugin/finished-tracks and
//      pushes the matched Subsonic IDs into the user-owned playlist via
//      host.SubsonicAPICall (which authenticates as that user automatically
//      thanks to the `users` permission).
package main

import (
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/navidrome/navidrome/plugins/pdk/go/host"
	"github.com/navidrome/navidrome/plugins/pdk/go/lifecycle"
	"github.com/navidrome/navidrome/plugins/pdk/go/pdk"
	"github.com/navidrome/navidrome/plugins/pdk/go/scheduler"
	"github.com/navidrome/navidrome/plugins/pdk/go/taskworker"

	"tonus-navidrome-plugin/internal/tasks"
)

// scheduleName is the cron-job key used both at registration and inside
// OnCallback to dispatch.
const scheduleName = "tonus-sync"

// Defaults mirror the JSON-Schema defaults; used when a key is set-but-empty.
const (
	defaultCron            = "0 7 * * *"
	defaultTopArtists      = 10
	defaultTracksPerArtist = 5
	defaultMaxQueuePerRun  = 30
	defaultPlaylistTpl     = "Discovery {date}"
)

type plugin struct{}

// userMapping mirrors the `users` array in the manifest config. Read out of
// pdk.GetConfig("users") which returns a JSON-string of this array.
type userMapping struct {
	NavidromeUsername    string `json:"navidrome_username"`
	ListenBrainzUsername string `json:"listenbrainz_username"`
	PlaylistName         string `json:"playlist_name"`
}

func getInt(key string, fallback int) int {
	raw, ok := pdk.GetConfig(key)
	if !ok || raw == "" {
		return fallback
	}
	v, err := strconv.Atoi(raw)
	if err != nil || v <= 0 {
		return fallback
	}
	return v
}

func loadUsers() ([]userMapping, error) {
	raw, ok := pdk.GetConfig("users")
	if !ok || raw == "" {
		return nil, fmt.Errorf("`users` config is empty — add at least one user mapping in the plugin settings")
	}
	var out []userMapping
	if err := json.Unmarshal([]byte(raw), &out); err != nil {
		return nil, fmt.Errorf("invalid `users` JSON: %w", err)
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("`users` array is empty")
	}
	return out, nil
}

// substituteTemplate replaces {date} → YYYY-MM-DD and {user} → navidromeUser.
// Done at enqueue-time (in OnCallback) so each task carries the resolved name
// — keeps the Reconcile-Task deterministic across multiple run-attempts.
func substituteTemplate(tpl, navidromeUser string) string {
	if tpl == "" {
		return ""
	}
	out := tpl
	out = strings.ReplaceAll(out, "{date}", time.Now().Format("2006-01-02"))
	out = strings.ReplaceAll(out, "{user}", navidromeUser)
	return out
}

// OnInit registers the cron and creates the task queue. We don't validate
// the config deeply here — defects surface at the first OnCallback.
func (p *plugin) OnInit() error {
	tonusURL, _ := pdk.GetConfig("tonus_url")
	cronExpr, ok := pdk.GetConfig("cron_expression")
	if !ok || cronExpr == "" {
		cronExpr = defaultCron
	}

	if tonusURL == "" {
		return fmt.Errorf("tonus_url is required (set it on the plugin settings page)")
	}

	// TaskQueue: concurrency 1 keeps user-runs serialized so the Subsonic
	// playlist-update is well-ordered and never races against itself.
	if err := host.TaskCreateQueue(tasks.QueueName, host.QueueConfig{
		Concurrency: 1,
		MaxRetries:  3,
		BackoffMs:   30_000,
		// RetentionMs is "how long terminated tasks are kept" — 1h is plenty
		// for log inspection without bloating the queue store.
		RetentionMs: 3600_000,
	}); err != nil {
		// CreateQueue is idempotent on most implementations; log but proceed.
		pdk.Log(pdk.LogWarn, "TaskCreateQueue: "+err.Error())
	}

	if _, err := host.SchedulerScheduleRecurring(cronExpr, scheduleName, scheduleName); err != nil {
		return fmt.Errorf("failed to schedule '%s' (%s): %w", scheduleName, cronExpr, err)
	}

	pdk.Log(pdk.LogInfo, fmt.Sprintf(
		"tonus-sync initialized: url=%s, cron=%q, queue=%s",
		tonusURL, cronExpr, tasks.QueueName,
	))
	return nil
}

// OnCallback (cron tick): enqueue trigger + reconcile per user, return fast.
func (p *plugin) OnCallback(req scheduler.SchedulerCallbackRequest) error {
	if req.ScheduleID != scheduleName && req.Payload != scheduleName {
		pdk.Log(pdk.LogWarn, "OnCallback got unexpected schedule: id="+req.ScheduleID+" payload="+req.Payload)
		return nil
	}

	tonusURL, _ := pdk.GetConfig("tonus_url")
	tonusToken, _ := pdk.GetConfig("tonus_token")

	users, err := loadUsers()
	if err != nil {
		pdk.Log(pdk.LogError, "tonus-sync: "+err.Error())
		return nil
	}

	topArtists := getInt("top_artists", defaultTopArtists)
	tracksPerArtist := getInt("tracks_per_artist", defaultTracksPerArtist)
	maxQueue := getInt("max_queue_per_run", defaultMaxQueuePerRun)

	enqueuedTrigger := 0
	enqueuedReconcile := 0

	for _, u := range users {
		if u.NavidromeUsername == "" || u.ListenBrainzUsername == "" {
			pdk.Log(pdk.LogWarn, fmt.Sprintf(
				"tonus-sync: skipping user mapping with missing fields (nd=%q, lb=%q)",
				u.NavidromeUsername, u.ListenBrainzUsername,
			))
			continue
		}
		playlistTpl := u.PlaylistName
		if playlistTpl == "" {
			playlistTpl = defaultPlaylistTpl
		}
		resolvedPlaylist := substituteTemplate(playlistTpl, u.NavidromeUsername)

		base := tasks.Job{
			NavidromeUser:    u.NavidromeUsername,
			ListenBrainzUser: u.ListenBrainzUsername,
			PlaylistName:     resolvedPlaylist,
			TonusURL:       tonusURL,
			TonusToken:     tonusToken,
			TopArtists:       topArtists,
			TracksPerArtist:  tracksPerArtist,
			MaxQueuePerRun:   maxQueue,
		}

		// 1) Trigger-task → backend kicks off discovery + download for this user.
		trigger := base
		trigger.Type = tasks.JobTriggerSync
		if payload, err := trigger.Marshal(); err == nil {
			if _, err := host.TaskEnqueue(tasks.QueueName, payload); err != nil {
				pdk.Log(pdk.LogError, fmt.Sprintf(
					"enqueue trigger %q: %s", u.NavidromeUsername, err.Error(),
				))
			} else {
				enqueuedTrigger++
			}
		}

		// 2) Reconcile-task → pull finished tracks + push to user playlist.
		// Catches tracks from earlier runs that finished in the meantime.
		reconcile := base
		reconcile.Type = tasks.JobReconcile
		if payload, err := reconcile.Marshal(); err == nil {
			if _, err := host.TaskEnqueue(tasks.QueueName, payload); err != nil {
				pdk.Log(pdk.LogError, fmt.Sprintf(
					"enqueue reconcile %q: %s", u.NavidromeUsername, err.Error(),
				))
			} else {
				enqueuedReconcile++
			}
		}
	}

	pdk.Log(pdk.LogInfo, fmt.Sprintf(
		"tonus-sync: enqueued %d trigger + %d reconcile tasks (users=%d)",
		enqueuedTrigger, enqueuedReconcile, len(users),
	))
	return nil
}

// OnTaskExecute is the taskworker hook — runs each enqueued job.
//
// Returns (string, error). A non-empty string is treated as an unrecoverable
// error (logged + dropped). A non-nil error triggers the queue's retry/backoff.
func (p *plugin) OnTaskExecute(req taskworker.TaskExecuteRequest) (string, error) {
	var job tasks.Job
	if err := json.Unmarshal(req.Payload, &job); err != nil {
		msg := fmt.Sprintf("OnTaskExecute: unmarshal failed: %v (payload=%s)", err, string(req.Payload))
		pdk.Log(pdk.LogError, msg)
		return msg, nil // unrecoverable — don't retry
	}
	return job.Dispatch()
}

func main() {}

func init() {
	p := &plugin{}
	lifecycle.Register(p)
	scheduler.Register(p)
	taskworker.Register(p)
}
