# MusicMe Subsonic Bridge

A lightweight bridge server that exposes MusicMe's music catalog via the Subsonic/OpenSubsonic API. Designed to be used with [Music Assistant](https://music-assistant.io/)'s OpenSubsonic provider.

## Quick Start

```bash
docker run -d --name musicme-bridge \
  -p 4533:4533 \
  -e MUSICME_EMAIL="your-email@example.com" \
  -e MUSICME_PASSWORD="your-password" \
  ghcr.io/juliendeveaux/musicme-subsonic-bridge:latest
```

Or with docker compose, create a `docker-compose.yml`:

```yaml
services:
  musicme-bridge:
    image: ghcr.io/juliendeveaux/musicme-subsonic-bridge:latest
    restart: unless-stopped
    ports:
      - "4533:4533"
    environment:
      MUSICME_EMAIL: "your-email@example.com"
      MUSICME_PASSWORD: "your-password"
```

Then `docker compose up -d`.

Then in Music Assistant, add an **OpenSubsonic** provider with:
- **Server**: `http://<your-docker-host>:4533`
- **Username**: `musicme` (or whatever you set in `SUBSONIC_USER`)
- **Password**: `musicme` (or whatever you set in `SUBSONIC_PASSWORD`)
- **Enable Legacy Authentication**: checked

## Features

- Search (artists, albums, tracks)
- Browse albums and artists
- Album track listing with cover art
- Audio streaming (AAC/MP4, 44.1kHz stereo)
- MusicMe thematic radios exposed as playlists
- Cover art proxying

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MUSICME_EMAIL` | Yes | - | Your MusicMe account email |
| `MUSICME_PASSWORD` | Yes | - | Your MusicMe account password |
| `SUBSONIC_USER` | No | `musicme` | Username for Subsonic auth |
| `SUBSONIC_PASSWORD` | No | `musicme` | Password for Subsonic auth |
| `PORT` | No | `4533` | Server port |
| `LOG_LEVEL` | No | `INFO` | Log level (DEBUG, INFO, WARNING, ERROR) |
