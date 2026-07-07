"""
Weekly refresh for the Brooklyn Current playlist.

Tracks a curated roster of Brooklyn-scene artists (see artists.txt,
edit it any time to add/remove names) rather than discovering artists
from national music blogs - those cover famous acts almost by
definition, not the local scene. For each tracked artist, checks
Spotify for anything released in the last LOOKBACK_DAYS days (capped
at MAX_TRACKS_PER_ARTIST tracks, so one prolific artist can't crowd
out the rest of the roster) and adds matches to the top of the
playlist. Older tracks are trimmed off the bottom once the playlist
exceeds PLAYLIST_CAP tracks.

Runs non-interactively using a refresh token (see get_refresh_token.py
for one-time setup). Required env vars: SPOTIFY_CLIENT_ID,
SPOTIFY_CLIENT_SECRET, SPOTIFY_REFRESH_TOKEN, SPOTIFY_PLAYLIST_ID.
"""

import os
import sys
import unicodedata
from datetime import date, datetime, timedelta

import spotipy
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyOAuth

SCOPE = "playlist-modify-public playlist-modify-private playlist-read-private user-follow-read"
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", 7))
PLAYLIST_CAP = int(os.environ.get("PLAYLIST_CAP", 100))
MAX_TRACKS_PER_ARTIST = int(os.environ.get("MAX_TRACKS_PER_ARTIST", 2))
ADD_BATCH_SIZE = 100

ARTISTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artists.txt")


def get_client() -> spotipy.Spotify:
    auth_manager = SpotifyOAuth(
        client_id=os.environ["SPOTIFY_CLIENT_ID"],
        client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
        redirect_uri=os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"),
        scope=SCOPE,
        cache_path=None,
    )
    token_info = auth_manager.refresh_access_token(os.environ["SPOTIFY_REFRESH_TOKEN"])
    # retries=0: some endpoints have returned very long Retry-After values
    # (hours, not seconds) under Development Mode quotas. spotipy's default
    # retry logic would sleep for that entire duration; we'd rather skip
    # and move on than have a weekly cron job hang for hours.
    return spotipy.Spotify(auth=token_info["access_token"], retries=0)


def load_tracked_artists() -> list[str]:
    with open(ARTISTS_FILE) as f:
        lines = [line.strip() for line in f]
    return [line for line in lines if line and not line.startswith("#")]


def normalize_name(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name)
    return decomposed.encode("ascii", "ignore").decode("ascii").lower()


def released_recently(release_date: str, precision: str, cutoff: date) -> bool:
    if precision != "day":
        return False
    return datetime.strptime(release_date, "%Y-%m-%d").date() >= cutoff


def get_new_tracks_for_artist(sp: spotipy.Spotify, name: str, cutoff: date) -> list[str]:
    # As of the Feb 2026 API changes this endpoint caps limit at 10.
    today = date.today()
    year_filter = str(cutoff.year) if cutoff.year == today.year else f"{cutoff.year}-{today.year}"
    query = f'artist:"{name}" year:{year_filter}'

    # (release_date, track) pairs - deduped by normalized title so
    # reissues/alternate versions of the same song don't each count as
    # a separate track, then capped to MAX_TRACKS_PER_ARTIST so one
    # prolific artist can't flood the playlist and crowd out others.
    candidates = {}
    offset = 0
    while True:
        try:
            results = sp.search(q=query, type="track", limit=10, offset=offset)
        except spotipy.SpotifyException as e:
            if e.http_status == 429:
                print(f"  rate-limited searching for {name!r}, skipping for this run")
                break
            raise

        tracks = results["tracks"]["items"]
        for track in tracks:
            if not any(normalize_name(a["name"]) == normalize_name(name) for a in track["artists"]):
                continue
            album = track["album"]
            if not released_recently(album["release_date"], album["release_date_precision"], cutoff):
                continue
            key = normalize_name(track["name"])
            if key not in candidates or album["release_date"] > candidates[key][0]:
                candidates[key] = (album["release_date"], track["uri"])

        if not results["tracks"].get("next") or len(tracks) < 10:
            break
        offset += 10

    ranked = sorted(candidates.values(), key=lambda pair: pair[0], reverse=True)
    return [uri for _, uri in ranked[:MAX_TRACKS_PER_ARTIST]]


def get_existing_track_uris(sp: spotipy.Spotify, playlist_id: str) -> set[str]:
    uris = set()
    offset = 0
    while True:
        page = sp.playlist_items(
            playlist_id, fields="items.item.uri,next", limit=100, offset=offset
        )
        uris.update(item["item"]["uri"] for item in page["items"] if item.get("item"))
        if not page.get("next"):
            break
        offset += 100
    return uris


def add_tracks_to_top(sp: spotipy.Spotify, playlist_id: str, track_uris: list[str]) -> None:
    # Each call inserts its whole batch at position 0, pushing prior
    # content down - so batches must be added in reverse order for the
    # overall result to preserve the original discovery order.
    chunks = [
        track_uris[i : i + ADD_BATCH_SIZE] for i in range(0, len(track_uris), ADD_BATCH_SIZE)
    ]
    for chunk in reversed(chunks):
        sp.playlist_add_items(playlist_id, chunk, position=0)


def trim_playlist(sp: spotipy.Spotify, playlist_id: str) -> None:
    total = sp.playlist_items(playlist_id, fields="total")["total"]
    overflow = total - PLAYLIST_CAP
    if overflow <= 0:
        return

    tail = sp.playlist_items(
        playlist_id,
        fields="items.item.uri",
        limit=overflow,
        offset=PLAYLIST_CAP,
    )["items"]
    items = [
        {"uri": item["item"]["uri"], "positions": [PLAYLIST_CAP + i]}
        for i, item in enumerate(tail)
        if item.get("item")
    ]
    if items:
        sp.playlist_remove_specific_occurrences_of_items(playlist_id, items)
        print(f"Trimmed {len(items)} track(s) off the end to stay under {PLAYLIST_CAP}.")


def main():
    load_dotenv()
    playlist_id = os.environ["SPOTIFY_PLAYLIST_ID"]
    sp = get_client()

    cutoff = date.today() - timedelta(days=LOOKBACK_DAYS)
    artists = load_tracked_artists()
    print(f"Checking {len(artists)} tracked artist(s) for new releases.")

    new_tracks = []
    seen = set()
    for name in artists:
        for uri in get_new_tracks_for_artist(sp, name, cutoff):
            if uri not in seen:
                seen.add(uri)
                new_tracks.append(uri)
    print(f"Found {len(new_tracks)} new track(s).")

    if not new_tracks:
        print("No qualifying new releases this week.")
        return

    existing = get_existing_track_uris(sp, playlist_id)
    to_add = [uri for uri in new_tracks if uri not in existing]

    if not to_add:
        print("All discovered tracks are already in the playlist.")
        return

    add_tracks_to_top(sp, playlist_id, to_add)
    print(f"Added {len(to_add)} new track(s).")

    trim_playlist(sp, playlist_id)


if __name__ == "__main__":
    if not os.environ.get("SPOTIFY_REFRESH_TOKEN"):
        sys.exit("SPOTIFY_REFRESH_TOKEN not set. Run get_refresh_token.py first.")
    main()
