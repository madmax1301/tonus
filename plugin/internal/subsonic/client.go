// Package subsonic wraps host.SubsonicAPICall for the operations the plugin
// needs: finding tracks by artist+title, finding/creating playlists, adding
// tracks idempotently.
//
// Auth model: each Client is bound to a Navidrome username. The host service
// (host.SubsonicAPICall) injects the right credentials based on the plugin's
// `users` permission — so we only need to pass `?u=USERNAME` in the URI and
// Navidrome resolves the rest. No passwords or tokens stored in the plugin.
package subsonic

import (
	"encoding/json"
	"fmt"
	"net/url"
	"strings"

	"github.com/navidrome/navidrome/plugins/pdk/go/host"
)

// Client is bound to a single Navidrome user — every Subsonic call is made
// "as" that user. Create one Client per user inside the plugin's task worker.
type Client struct {
	Username string
}

func New(navidromeUsername string) *Client {
	return &Client{Username: strings.TrimSpace(navidromeUsername)}
}

// ---- Generic call ---------------------------------------------------------

// call builds a Subsonic URI with the user injected, parses the JSON wrapper,
// and returns the inner subsonic-response object on success.
//
// `endpoint` is e.g. "createPlaylist" (without .view, without leading slash).
// `params` is the query map; `u` is added automatically.
func (c *Client) call(endpoint string, params url.Values) (json.RawMessage, error) {
	if c.Username == "" {
		return nil, fmt.Errorf("subsonic: no username bound")
	}
	if params == nil {
		params = url.Values{}
	}
	params.Set("u", c.Username)
	uri := "/rest/" + endpoint + "?" + params.Encode()

	respJSON, err := host.SubsonicAPICall(uri)
	if err != nil {
		return nil, fmt.Errorf("subsonic %s: %w", endpoint, err)
	}

	// Subsonic responses are wrapped in {"subsonic-response": {...}}.
	var wrapper struct {
		SubsonicResponse json.RawMessage `json:"subsonic-response"`
	}
	if err := json.Unmarshal([]byte(respJSON), &wrapper); err != nil {
		return nil, fmt.Errorf("subsonic %s: parse wrapper: %w", endpoint, err)
	}

	// Inspect status + error in the inner object.
	var status struct {
		Status string `json:"status"`
		Error  struct {
			Code    int    `json:"code"`
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.Unmarshal(wrapper.SubsonicResponse, &status); err != nil {
		return nil, fmt.Errorf("subsonic %s: parse status: %w", endpoint, err)
	}
	if status.Status != "ok" {
		return nil, fmt.Errorf("subsonic %s: %d %s", endpoint, status.Error.Code, status.Error.Message)
	}
	return wrapper.SubsonicResponse, nil
}

// ---- Search ---------------------------------------------------------------

// SearchTrackID resolves an artist+title pair to a Subsonic track ID.
// Returns "" if no plausible match was found in the user's library.
//
// Heuristic: top 10 song-results from search3 are scanned, the first whose
// artist + title both substring-match (case-insensitive) wins.
func (c *Client) SearchTrackID(artist, title string) (string, error) {
	if artist == "" || title == "" {
		return "", nil
	}
	q := url.Values{}
	q.Set("query", strings.TrimSpace(artist+" "+title))
	q.Set("songCount", "10")
	q.Set("albumCount", "0")
	q.Set("artistCount", "0")

	body, err := c.call("search3", q)
	if err != nil {
		return "", err
	}

	var search struct {
		SearchResult3 struct {
			Song []struct {
				ID     string `json:"id"`
				Title  string `json:"title"`
				Artist string `json:"artist"`
			} `json:"song"`
		} `json:"searchResult3"`
	}
	if err := json.Unmarshal(body, &search); err != nil {
		return "", fmt.Errorf("subsonic search3: parse result: %w", err)
	}

	aLow := strings.ToLower(strings.TrimSpace(artist))
	tLow := strings.ToLower(strings.TrimSpace(title))
	for _, s := range search.SearchResult3.Song {
		sa := strings.ToLower(s.Artist)
		st := strings.ToLower(s.Title)
		if (strings.Contains(sa, aLow) || strings.Contains(aLow, sa)) &&
			(strings.Contains(st, tLow) || strings.Contains(tLow, st)) {
			return s.ID, nil
		}
	}
	return "", nil
}

// ---- Playlists ------------------------------------------------------------

// PlaylistRef is the minimal info we need: id + existing track-IDs (for dedupe).
type PlaylistRef struct {
	ID         string
	Name       string
	ExistingIDs map[string]struct{}
}

// FindPlaylistByName looks up a playlist owned by this user with exact name match.
// Returns nil if not found. Also fills ExistingIDs by issuing a getPlaylist call.
func (c *Client) FindPlaylistByName(name string) (*PlaylistRef, error) {
	body, err := c.call("getPlaylists", nil)
	if err != nil {
		return nil, err
	}
	var lst struct {
		Playlists struct {
			Playlist []struct {
				ID   string `json:"id"`
				Name string `json:"name"`
			} `json:"playlist"`
		} `json:"playlists"`
	}
	if err := json.Unmarshal(body, &lst); err != nil {
		return nil, fmt.Errorf("subsonic getPlaylists: parse: %w", err)
	}
	for _, p := range lst.Playlists.Playlist {
		if p.Name == name {
			ids, err := c.playlistTrackIDs(p.ID)
			if err != nil {
				return nil, err
			}
			return &PlaylistRef{ID: p.ID, Name: p.Name, ExistingIDs: ids}, nil
		}
	}
	return nil, nil
}

func (c *Client) playlistTrackIDs(playlistID string) (map[string]struct{}, error) {
	q := url.Values{}
	q.Set("id", playlistID)
	body, err := c.call("getPlaylist", q)
	if err != nil {
		return nil, err
	}
	var pl struct {
		Playlist struct {
			Entry []struct {
				ID string `json:"id"`
			} `json:"entry"`
		} `json:"playlist"`
	}
	if err := json.Unmarshal(body, &pl); err != nil {
		return nil, fmt.Errorf("subsonic getPlaylist: parse: %w", err)
	}
	ids := make(map[string]struct{}, len(pl.Playlist.Entry))
	for _, e := range pl.Playlist.Entry {
		if e.ID != "" {
			ids[e.ID] = struct{}{}
		}
	}
	return ids, nil
}

// CreatePlaylist creates a new playlist owned by the bound user.
// `songIDs` may be empty (then the playlist starts empty).
// Returns the new playlist ID.
func (c *Client) CreatePlaylist(name string, songIDs []string) (*PlaylistRef, error) {
	q := url.Values{}
	q.Set("name", name)
	for _, id := range songIDs {
		q.Add("songId", id)
	}
	body, err := c.call("createPlaylist", q)
	if err != nil {
		return nil, err
	}
	var resp struct {
		Playlist struct {
			ID string `json:"id"`
		} `json:"playlist"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		return nil, fmt.Errorf("subsonic createPlaylist: parse: %w", err)
	}
	if resp.Playlist.ID == "" {
		return nil, fmt.Errorf("subsonic createPlaylist: no id in response")
	}
	existing := make(map[string]struct{}, len(songIDs))
	for _, id := range songIDs {
		existing[id] = struct{}{}
	}
	return &PlaylistRef{ID: resp.Playlist.ID, Name: name, ExistingIDs: existing}, nil
}

// AddTracksIdempotent appends song-IDs to an existing playlist, filtering out
// IDs that are already present. Returns how many were actually added.
//
// Uses Subsonic `updatePlaylist` with `songIdToAdd` per ID (Subsonic supports
// multiple).
func (c *Client) AddTracksIdempotent(pl *PlaylistRef, songIDs []string) (int, error) {
	if pl == nil || pl.ID == "" {
		return 0, fmt.Errorf("subsonic addTracks: nil playlist")
	}
	toAdd := make([]string, 0, len(songIDs))
	for _, id := range songIDs {
		if id == "" {
			continue
		}
		if _, dup := pl.ExistingIDs[id]; dup {
			continue
		}
		toAdd = append(toAdd, id)
	}
	if len(toAdd) == 0 {
		return 0, nil
	}
	q := url.Values{}
	q.Set("playlistId", pl.ID)
	for _, id := range toAdd {
		q.Add("songIdToAdd", id)
	}
	if _, err := c.call("updatePlaylist", q); err != nil {
		return 0, err
	}
	// Update the in-memory set so subsequent calls within the same task
	// don't re-add.
	for _, id := range toAdd {
		pl.ExistingIDs[id] = struct{}{}
	}
	return len(toAdd), nil
}
