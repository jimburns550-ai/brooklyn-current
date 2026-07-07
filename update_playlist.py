"""
Weekly refresh for the Brooklyn Current playlist.

Two phases:
  1. find_new_releases() scans BrooklynVegan, Stereogum, and Bandcamp
     Daily's RSS feeds for release-announcement posts and parses each
     into a (candidate artist names, track title) pair. No Spotify
     calls happen here.
  2. find_track_uri() looks each release up on Spotify by exact
     track+artist search and, if found, the track is added to the top
     of the playlist. Older tracks are trimmed off the bottom once the
     playlist exceeds PLAYLIST_CAP tracks.

Runs non-interactively using a refresh token (see get_refresh_token.py
for one-time setup). Required env vars: SPOTIFY_CLIENT_ID,
SPOTIFY_CLIENT_SECRET, SPOTIFY_REFRESH_TOKEN, SPOTIFY_PLAYLIST_ID.
"""

import html
import os
import re
import ssl
import sys
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta

import certifi
import spotipy
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyOAuth

SCOPE = "playlist-modify-public playlist-modify-private playlist-read-private user-follow-read"
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", 7))
PLAYLIST_CAP = int(os.environ.get("PLAYLIST_CAP", 100))
ADD_BATCH_SIZE = 100

FEEDS = [
    "https://www.brooklynvegan.com/feed/",
    "https://www.stereogum.com/category/music/feed/",
    "https://daily.bandcamp.com/feed",
]

QUOTE_CAPTURE_RE = re.compile(r'["“]([^"”]+)["”]')
# Bandcamp Daily's format: 'Artist(s), "Track Title"'
COMMA_QUOTE_RE = re.compile(r'^(.+?),\s*["“]')
# Stereogum's dominant format: 'Artist Name – "Track Title"'
DASH_RE = re.compile(r"^(.+?)\s+[–—]\s+")
# BrooklynVegan-style release posts: 'Artist Name announce/share ... "Track"'
RELEASE_VERB_RE = re.compile(
    r"^(.+?)\s+(?:announces?|shares?|releas\w*|drops?|unveils?|premieres?)\b",
    re.IGNORECASE,
)
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


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


def fetch_feed_titles(url: str) -> list[str]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15, context=SSL_CONTEXT) as resp:
        root = ET.fromstring(resp.read())
    return [item.findtext("title", "") for item in root.iter("item")]


def extract_release(raw_title: str) -> tuple[list[str], str] | None:
    """Parse a blog post title into (candidate artist names, track title).

    Requires a quoted segment (the track/song title) to be present at
    all - this alone filters out most tour announcements, recaps, and
    roundup posts that aren't about a specific new release.
    """
    title = html.unescape(raw_title)

    quote_match = QUOTE_CAPTURE_RE.search(title)
    if not quote_match:
        return None
    track_title = quote_match.group(1).strip().rstrip(",.:")

    match = COMMA_QUOTE_RE.match(title) or DASH_RE.match(title) or RELEASE_VERB_RE.match(title)
    if not match:
        return None

    artist_part = match.group(1).strip()
    artists = [
        part.strip()
        for part in re.split(r"\s*&\s*|,\s*", artist_part)
        if part.strip() and part.strip().lower() != "various artists"
    ]
    if not artists:
        return None
    return artists, track_title


def find_new_releases() -> list[tuple[list[str], str]]:
    """Scan the blog feeds and return (candidate artists, track title) pairs.

    Pure blog-scraping step - no Spotify calls here at all.
    """
    releases = []
    seen = set()
    for feed_url in FEEDS:
        for title in fetch_feed_titles(feed_url):
            release = extract_release(title)
            if release and (key := (tuple(release[0]), release[1])) not in seen:
                seen.add(key)
                releases.append(release)
    return releases


def normalize_name(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name)
    return decomposed.encode("ascii", "ignore").decode("ascii").lower()


def released_recently(release_date: str, precision: str, cutoff: date) -> bool:
    if precision != "day":
        return False
    return datetime.strptime(release_date, "%Y-%m-%d").date() >= cutoff


def find_track_uri(sp: spotipy.Spotify, artists: list[str], track_title: str, cutoff: date) -> str | None:
    """Look up a specific blog-mentioned release on Spotify.

    Tries each candidate artist name (a post can credit multiple
    collaborators) until one produces an exact track+artist match whose
    release date is plausibly recent - a sanity check against search
    matching an older reissue or an unrelated same-titled track.
    """
    for name in artists:
        try:
            results = sp.search(q=f'track:"{track_title}" artist:"{name}"', type="track", limit=5)
        except spotipy.SpotifyException as e:
            if e.http_status == 429:
                print(f"  rate-limited searching for {name!r} - {track_title!r}, skipping")
                return None
            raise

        for track in results["tracks"]["items"]:
            if not any(normalize_name(a["name"]) == normalize_name(name) for a in track["artists"]):
                continue
            album = track["album"]
            if released_recently(album["release_date"], album["release_date_precision"], cutoff):
                return track["uri"]
    return None


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
    releases = find_new_releases()
    print(f"Parsed {len(releases)} candidate release(s) from blog feeds.")

    new_tracks = []
    seen = set()
    for artists, track_title in releases:
        uri = find_track_uri(sp, artists, track_title, cutoff)
        if uri and uri not in seen:
            seen.add(uri)
            new_tracks.append(uri)
    print(f"Matched {len(new_tracks)} release(s) to a Spotify track.")

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
