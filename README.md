# Brooklyn Current

Weekly Spotify playlist of up-and-coming artist releases, refreshed via a scheduled GitHub Action.

## One-time local setup

1. Create an app at the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
   - Add a Redirect URI of `http://127.0.0.1:8888/callback` (must be a loopback IP literal, not `localhost`).
2. `python -m venv venv && source venv/bin/activate`
3. `pip install -r requirements.txt`
4. `cp .env.example .env` and fill in `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET`.
5. `python get_refresh_token.py`
   - Opens your browser, you log in and approve access.
   - Creates the "Brooklyn Current" playlist (or reuses it if it already exists).
   - Saves `SPOTIFY_REFRESH_TOKEN` and `SPOTIFY_PLAYLIST_ID` into `.env`.

## Push to GitHub

```
git init
git add .
git commit -m "Set up Brooklyn Current OAuth + playlist creation"
git remote add origin <your-repo-url>
git push -u origin main
```

`.env` is gitignored — never commit it.

## GitHub Actions secrets

In the repo: Settings > Secrets and variables > Actions > New repository secret. Add:

- `SPOTIFY_CLIENT_ID`
- `SPOTIFY_CLIENT_SECRET`
- `SPOTIFY_REFRESH_TOKEN`
- `SPOTIFY_PLAYLIST_ID`

The workflow at `.github/workflows/update-playlist.yml` runs every Monday at 13:00 UTC (and can be triggered manually from the Actions tab) and calls `update_playlist.py`.

## How the weekly update works

`update_playlist.py` runs in two phases:

1. **Find releases** — scans the RSS feeds of BrooklynVegan, Stereogum, and Bandcamp Daily for release-announcement posts, parsing each into a candidate artist name + track title.
2. **Match and add** — looks each release up on Spotify with an exact track+artist search, and adds any match to the top of the playlist. Tracks fall off the bottom once the playlist exceeds 100 (`PLAYLIST_CAP`).

No follower-count filtering is applied — Spotify removed the `followers`/`popularity` fields from the Artist object in its February 2026 API changes, so "up-and-coming" is defined entirely by blog curation rather than audience size.
