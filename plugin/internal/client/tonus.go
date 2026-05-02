// Package client wraps host.HTTPSend to talk to the tonus FastAPI backend.
//
// All requests:
//   - target the configured BaseURL (no trailing slash)
//   - send User-Agent identifying the plugin
//   - attach `Authorization: Bearer <token>` if the plugin config has one set
//   - have a 30-second timeout (tonus csv-import polls can take a moment)
//
// Non-2xx responses are returned as errors; the body is included (truncated
// to 200 chars) so the caller can surface it to the plugin's KVStore status.
package client

import (
	"encoding/json"
	"fmt"
	"net/url"
	"strings"

	"github.com/navidrome/navidrome/plugins/pdk/go/host"
)

const (
	userAgent     = "tonus-navidrome-plugin/0.1"
	defaultTimeoutMs = int32(30000)
)

// ---- Response shapes ------------------------------------------------------

type HealthResponse struct {
	OK           bool   `json:"ok"`
	Version      string `json:"version"`
	AuthRequired bool   `json:"auth_required"`
	NowMs        int64  `json:"now_ms"`
}

// TrackHint mirrors what the tonus backend includes per item in the
// /api/plugin/library/missing response. We pass it back unchanged on the
// /api/download POST so the queue UI can display cover art etc. immediately,
// without waiting for the metadata fetch.
type TrackHint struct {
	ID       string `json:"id"`
	Name     string `json:"name"`
	Artist   string `json:"artist"`
	Album    string `json:"album"`
	AlbumArt string `json:"album_art,omitempty"`
}

type MissingItem struct {
	Artist    string    `json:"artist"`
	Title     string    `json:"title"`
	TrackID   string    `json:"track_id"`
	TrackHint TrackHint `json:"track_hint"`
}

type LibraryMissingResponse struct {
	Items  []MissingItem `json:"items"`
	Count  int           `json:"count"`
	Source string        `json:"source"`
}

// DownloadResponse covers the success case of POST /api/download. The 409
// "already in queue/library" path is detected by status code, not by body.
type DownloadResponse struct {
	Status  string `json:"status"`
	Message string `json:"message"`
	TrackID string `json:"track_id"`
}

type CSVImportResponse struct {
	Status string `json:"status"`
	JobID  string `json:"job_id"`
	Total  int    `json:"total"`
}

type CSVStatusResponse struct {
	Status    string `json:"status"`
	Total     int    `json:"total"`
	Processed int    `json:"processed"`
	Found     int    `json:"found"`
	NotFound  int    `json:"not_found"`
	Message   string `json:"message"`
}

// CSVQueueAllResponse mirrors the FastAPI return shape (see app.py
// `csv_queue_all`): no `ok` field, separate counters for dup/error/total.
type CSVQueueAllResponse struct {
	Queued           int `json:"queued"`
	SkippedDuplicate int `json:"skipped_duplicate"`
	Errors           int `json:"errors"`
	TotalMatched     int `json:"total_matched"`
}

type SyncStatusResponse struct {
	Queue struct {
		Queued     int     `json:"queued"`
		Processing int     `json:"processing"`
		Completed  int     `json:"completed"`
		Error      int     `json:"error"`
		OldestAgeS float64 `json:"oldest_age_s"`
	} `json:"queue"`
	LastCompletedMs   int64  `json:"last_completed_ms"`
	LastErrorMs       int64  `json:"last_error_ms"`
	LastErrorMessage  string `json:"last_error_message"`
	NowMs             int64  `json:"now_ms"`
	AuthRequired      bool   `json:"auth_required"`
}

// ---- Client ---------------------------------------------------------------

type Client struct {
	BaseURL string
	Token   string
}

func New(baseURL, token string) *Client {
	return &Client{
		BaseURL: strings.TrimRight(strings.TrimSpace(baseURL), "/"),
		Token:   strings.TrimSpace(token),
	}
}

// request issues an HTTP call and decodes JSON into out (when not nil).
// Returns an error for transport failures and for non-2xx status codes
// (with a snippet of the body included).
func (c *Client) request(method, path string, body []byte, out interface{}) error {
	headers := map[string]string{
		"User-Agent": userAgent,
	}
	if c.Token != "" {
		headers["Authorization"] = "Bearer " + c.Token
	}
	if body != nil {
		headers["Content-Type"] = "application/json"
	}
	req := host.HTTPRequest{
		Method:    method,
		URL:       c.BaseURL + path,
		Headers:   headers,
		Body:      body,
		TimeoutMs: defaultTimeoutMs,
	}
	resp, err := host.HTTPSend(req)
	if err != nil {
		return err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		snippet := string(resp.Body)
		if len(snippet) > 200 {
			snippet = snippet[:200]
		}
		return fmt.Errorf("tonus %s %s: HTTP %d %s",
			method, path, resp.StatusCode, snippet)
	}
	if out != nil && len(resp.Body) > 0 {
		if err := json.Unmarshal(resp.Body, out); err != nil {
			return fmt.Errorf("decode response from %s: %w", path, err)
		}
	}
	return nil
}

// ---- Endpoints ------------------------------------------------------------

func (c *Client) Health() (*HealthResponse, error) {
	out := &HealthResponse{}
	if err := c.request("GET", "/api/plugin/health", nil, out); err != nil {
		return nil, err
	}
	return out, nil
}

func (c *Client) LibraryMissing(lbUser string, topN, perArtist, maxTotal int) (*LibraryMissingResponse, error) {
	q := url.Values{}
	q.Set("listenbrainz_user", lbUser)
	q.Set("top_artists", fmt.Sprintf("%d", topN))
	q.Set("tracks_per_artist", fmt.Sprintf("%d", perArtist))
	q.Set("max_total", fmt.Sprintf("%d", maxTotal))
	out := &LibraryMissingResponse{}
	if err := c.request("GET", "/api/plugin/library/missing?"+q.Encode(), nil, out); err != nil {
		return nil, err
	}
	return out, nil
}

func (c *Client) ImportCSV(csvText, provider string, limit int) (*CSVImportResponse, error) {
	if provider == "" {
		provider = "deezer"
	}
	if limit <= 0 {
		limit = 3
	}
	body, err := json.Marshal(map[string]interface{}{
		"csv_text": csvText,
		"provider": provider,
		"limit":    limit,
	})
	if err != nil {
		return nil, fmt.Errorf("marshal CSV-import body: %w", err)
	}
	out := &CSVImportResponse{}
	if err := c.request("POST", "/api/import/csv", body, out); err != nil {
		return nil, err
	}
	return out, nil
}

func (c *Client) CSVStatus(jobID string) (*CSVStatusResponse, error) {
	out := &CSVStatusResponse{}
	if err := c.request("GET", "/api/import/csv/status/"+url.PathEscape(jobID), nil, out); err != nil {
		return nil, err
	}
	return out, nil
}

func (c *Client) CSVQueueAll(jobID, location string) (*CSVQueueAllResponse, error) {
	if location == "" {
		location = "navidrome"
	}
	body, err := json.Marshal(map[string]interface{}{
		"provider": "deezer",
		"location": location,
	})
	if err != nil {
		return nil, fmt.Errorf("marshal queue-all body: %w", err)
	}
	out := &CSVQueueAllResponse{}
	if err := c.request("POST", "/api/import/csv/queue-all/"+url.PathEscape(jobID), body, out); err != nil {
		return nil, err
	}
	return out, nil
}

func (c *Client) SyncStatus() (*SyncStatusResponse, error) {
	out := &SyncStatusResponse{}
	if err := c.request("GET", "/api/plugin/sync-status", nil, out); err != nil {
		return nil, err
	}
	return out, nil
}

// TriggerSyncRequest mirrors backend `PluginSyncRequest`. PlaylistName + NavidromeUser
// are optional; if NavidromeUser is set, finished tracks become discoverable via
// /api/plugin/finished-tracks for plugin-side per-user playlist creation.
// PlaylistName supports the {date} placeholder — backend substitutes it before use.
type TriggerSyncRequest struct {
	ListenBrainzUser string `json:"listenbrainz_user"`
	TopArtists       int    `json:"top_artists"`
	TracksPerArtist  int    `json:"tracks_per_artist"`
	HistoryDays      int    `json:"history_days,omitempty"`
	MaxTotal         int    `json:"max_total"`
	Location         string `json:"location,omitempty"`
	PlaylistName     string `json:"playlist_name,omitempty"`
	NavidromeUser    string `json:"navidrome_user,omitempty"`
}

// FinishedTrack mirrors backend `/api/plugin/finished-tracks` items. The
// SubsonicTrackID is resolved server-side (admin-auth search3) and cached in
// the job payload — when present, the plugin can skip its own search3 call.
type FinishedTrack struct {
	DeezerID         string `json:"deezer_id"`
	Artist           string `json:"artist"`
	Title            string `json:"title"`
	PlaylistName     string `json:"playlist_name"`
	CompletedAtMs    int64  `json:"completed_at_ms"`
	SubsonicTrackID  string `json:"subsonic_track_id"`
}

type FinishedTracksResponse struct {
	Items          []FinishedTrack `json:"items"`
	Count          int             `json:"count"`
	NavidromeUser  string          `json:"navidrome_user"`
	SinceDays      int             `json:"since_days"`
}

type TriggerSyncResponse struct {
	Started bool   `json:"started"`
	Message string `json:"message"`
}

// TriggerSync fires the backend's POST /api/plugin/sync. Backend runs the
// discovery+queue pipeline asynchronously and returns 200 immediately —
// keeping us inside Navidrome's plugin-callback timeout.
func (c *Client) TriggerSync(req TriggerSyncRequest) (*TriggerSyncResponse, error) {
	body, err := json.Marshal(req)
	if err != nil {
		return nil, fmt.Errorf("marshal sync trigger: %w", err)
	}
	out := &TriggerSyncResponse{}
	if err := c.request("POST", "/api/plugin/sync", body, out); err != nil {
		return nil, err
	}
	return out, nil
}

// DownloadOutcome describes the result of a single Download call. We don't
// surface non-success status codes as errors because /api/download returns
// 409 for "already in queue/library" which is an expected, common outcome.
type DownloadOutcome int

const (
	DownloadQueued     DownloadOutcome = iota // 200/201 — newly queued
	DownloadDuplicate                         // 409    — already known/queued
	DownloadFailed                            // anything else; check Err / Status
)

// DownloadResult bundles outcome + diagnostics for the runner's status block.
type DownloadResult struct {
	Outcome DownloadOutcome
	Status  int    // HTTP status code from tonus
	Message string // body snippet (only on Failed) or tonus's status message
	Err     error  // transport-level error (timeout, DNS, etc.)
}

// Download triggers POST /api/download for one track. Bypasses the generic
// request() helper because we need bespoke handling for 409 (= duplicate).
func (c *Client) Download(trackID string, hint TrackHint, location string) DownloadResult {
	if location == "" {
		location = "navidrome"
	}
	body, err := json.Marshal(map[string]interface{}{
		"track_id":   trackID,
		"location":   location,
		"provider":   "deezer",
		"track_hint": hint,
	})
	if err != nil {
		return DownloadResult{Outcome: DownloadFailed, Err: fmt.Errorf("marshal: %w", err)}
	}
	headers := map[string]string{
		"User-Agent":   userAgent,
		"Content-Type": "application/json",
	}
	if c.Token != "" {
		headers["Authorization"] = "Bearer " + c.Token
	}
	resp, err := host.HTTPSend(host.HTTPRequest{
		Method:    "POST",
		URL:       c.BaseURL + "/api/download",
		Headers:   headers,
		Body:      body,
		TimeoutMs: defaultTimeoutMs,
	})
	if err != nil {
		return DownloadResult{Outcome: DownloadFailed, Err: err}
	}
	status := int(resp.StatusCode)
	switch {
	case status >= 200 && status < 300:
		var dr DownloadResponse
		_ = json.Unmarshal(resp.Body, &dr) // best-effort
		return DownloadResult{Outcome: DownloadQueued, Status: status, Message: dr.Message}
	case status == 409:
		// tonus verwendet 409 für "schon in Queue/Library/heruntergeladen" —
		// bewusst unkritisch behandeln.
		snippet := string(resp.Body)
		if len(snippet) > 200 {
			snippet = snippet[:200]
		}
		return DownloadResult{Outcome: DownloadDuplicate, Status: status, Message: snippet}
	default:
		snippet := string(resp.Body)
		if len(snippet) > 200 {
			snippet = snippet[:200]
		}
		return DownloadResult{Outcome: DownloadFailed, Status: status, Message: snippet}
	}
}

// FinishedTracks queries the backend for tracks of a given Navidrome user
// that finished downloading within the last `sinceDays` days. Used by the
// plugin's reconcile-task to populate per-user Subsonic playlists.
//
// playlistName is optional — when set, only tracks tagged with that playlist
// are returned (useful when reconciling a single specific playlist).
func (c *Client) FinishedTracks(navidromeUser string, sinceDays int, playlistName string) (*FinishedTracksResponse, error) {
	if sinceDays <= 0 {
		sinceDays = 60
	}
	q := url.Values{}
	q.Set("navidrome_user", navidromeUser)
	q.Set("since_days", fmt.Sprintf("%d", sinceDays))
	if playlistName != "" {
		q.Set("playlist_name", playlistName)
	}
	out := &FinishedTracksResponse{}
	if err := c.request("GET", "/api/plugin/finished-tracks?"+q.Encode(), nil, out); err != nil {
		return nil, err
	}
	return out, nil
}
