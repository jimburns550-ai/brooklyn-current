"""
One-time interactive setup for Brooklyn Current.

Run this locally (not in CI) to:
  1. Complete the Spotify OAuth Authorization Code flow in your browser.
  2. Print/save a long-lived refresh token that the scheduled GitHub Action
     will use to authenticate without any further browser interaction.
  3. Create the destination playlist (if it doesn't already exist) and
     save its playlist ID.

Usage:
    python get_refresh_token.py
    python get_refresh_token.py --playlist-name "Brooklyn Current" --public

Requires SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REDIRECT_URI
to be set in a .env file (see .env.example). SPOTIFY_REDIRECT_URI must
exactly match a Redirect URI registered on your app at
https://developer.spotify.com/dashboard, and must use a loopback IP
literal (e.g. http://127.0.0.1:8888/callback) rather than "localhost".
"""

import argparse
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

from dotenv import set_key, load_dotenv
import spotipy
from spotipy.oauth2 import SpotifyOAuth

SCOPE = "playlist-modify-public playlist-modify-private playlist-read-private user-follow-read"
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


class _CallbackHandler(BaseHTTPRequestHandler):
    """Captures the single OAuth redirect and stashes the query string."""

    result = {}

    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        _CallbackHandler.result = query

        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        if "code" in query:
            body = "<html><body><h2>Success! You can close this tab and return to the terminal.</h2></body></html>"
        else:
            body = "<html><body><h2>Something went wrong. Check the terminal for details.</h2></body></html>"
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, format, *args):  # noqa: A002 - silence default logging
        pass


def wait_for_auth_code(redirect_uri: str) -> str:
    parsed = urlparse(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8888

    server = HTTPServer((host, port), _CallbackHandler)
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    thread.join(timeout=180)

    if "code" not in _CallbackHandler.result:
        error = _CallbackHandler.result.get("error", ["timed out waiting for redirect"])
        sys.exit(f"Authorization failed: {error[0]}")

    return _CallbackHandler.result["code"][0]


def get_refresh_token(auth_manager: SpotifyOAuth) -> dict:
    auth_url = auth_manager.get_authorize_url()
    print(f"Opening browser for Spotify login:\n{auth_url}\n")
    webbrowser.open(auth_url)

    code = wait_for_auth_code(auth_manager.redirect_uri)
    token_info = auth_manager.get_access_token(code, as_dict=True, check_cache=False)
    return token_info


def get_or_create_playlist(sp: spotipy.Spotify, name: str, description: str, public: bool) -> str:
    existing = None
    results = sp.current_user_playlists(limit=50)
    while results:
        existing = next((p for p in results["items"] if p["name"] == name), None)
        if existing or not results.get("next"):
            break
        results = sp.next(results)

    if existing:
        print(f"Found existing playlist '{name}' ({existing['id']}), reusing it.")
        return existing["id"]

    playlist = sp.current_user_playlist_create(
        name, public=public, description=description
    )
    print(f"Created playlist '{name}' ({playlist['id']}).")
    return playlist["id"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--playlist-name", default="Brooklyn Current")
    parser.add_argument(
        "--playlist-description",
        default="Weekly rotating playlist of up-and-coming artist releases.",
    )
    parser.add_argument(
        "--public", action="store_true", help="Make the playlist public (default: private)"
    )
    args = parser.parse_args()

    load_dotenv(ENV_PATH)
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    redirect_uri = os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")

    if not client_id or not client_secret:
        sys.exit(
            "SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET must be set in .env "
            "(copy .env.example to .env and fill them in)."
        )

    auth_manager = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=SCOPE,
        cache_path=None,
        open_browser=False,
    )

    token_info = get_refresh_token(auth_manager)
    refresh_token = token_info["refresh_token"]

    sp = spotipy.Spotify(auth=token_info["access_token"])
    playlist_id = get_or_create_playlist(
        sp, args.playlist_name, args.playlist_description, args.public
    )

    if not os.path.exists(ENV_PATH):
        open(ENV_PATH, "a").close()
    set_key(ENV_PATH, "SPOTIFY_REFRESH_TOKEN", refresh_token)
    set_key(ENV_PATH, "SPOTIFY_PLAYLIST_ID", playlist_id)

    print("\nDone. Saved SPOTIFY_REFRESH_TOKEN and SPOTIFY_PLAYLIST_ID to .env")
    print("\nAdd these as GitHub Actions secrets (Settings > Secrets and variables > Actions):")
    print(f"  SPOTIFY_CLIENT_ID     = {client_id}")
    print("  SPOTIFY_CLIENT_SECRET = (from your .env, keep it secret)")
    print(f"  SPOTIFY_REFRESH_TOKEN = {refresh_token}")
    print(f"  SPOTIFY_PLAYLIST_ID   = {playlist_id}")
    print("\nDo not commit your .env file.")


if __name__ == "__main__":
    main()
