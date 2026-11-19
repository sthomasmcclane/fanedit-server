# FanEdits Server

Lightweight, read-only, NFO-driven media server for browsing and streaming a folder-organized FanEdits collection.

## Features
- Mirrors the directory structure under `MEDIA_ROOT` (`/data/fanedits`) without opinionated movie/TV scanner rules.
- Reads Kodi-style `.nfo` metadata (`<movie>`, `<episodedetails>`, `<tvshow>`, `<folder>`) and falls back gracefully to filenames.
- Automatically pairs `.jpg`, `.png`, and `.webp` cover art with videos and folders.
- Streams browser-compatible formats with HTTP `Range` seeking, and provides `.m3u` ("Open in VLC") and download links for other formats (e.g., `.avi`).

## Docker Compose Example

```yaml
services:
  fanedits:
    build: .
    image: fanedits-server:local
    container_name: fanedits
    user: "1000:1000"
    environment:
      - TZ=Australia/Brisbane
      - MEDIA_ROOT=/data/fanedits
    volumes:
      - /mnt/media/Film and TV/FanEdits:/data/fanedits:ro
    ports:
      - 8090:8090
    restart: no
```
