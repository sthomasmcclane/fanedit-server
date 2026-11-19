"""Lightweight, read-only, NFO-driven media server for FanMedia (FanEdits & FanFilms)."""

from __future__ import annotations

import html
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

_raw_roots = os.environ.get("MEDIA_ROOTS") or os.environ.get("MEDIA_ROOT", "/data/fanedits")
MEDIA_ROOTS = [Path(p.strip()).resolve() for p in _raw_roots.split(",") if p.strip()]
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".m4v", ".webm"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
BROWSER_PLAYABLE_EXTS = {".mp4", ".m4v", ".webm", ".mkv"}

app = FastAPI(title="FanMedia")


@dataclass
class NfoMeta:
    title: str = ""
    year: str = ""
    plot: str = ""
    director: str = ""
    genres: list[str] = field(default_factory=list)
    season: str = ""
    episode: str = ""


def _safe_rel(rel_path: str) -> str:
    clean = rel_path.strip("/")
    parts = Path(clean).parts if clean else ()
    if any(p in ("..", ".") for p in parts):
        raise HTTPException(status_code=403, detail="Forbidden")
    return clean


def _resolve_dirs(rel_path: str) -> list[Path]:
    clean = _safe_rel(rel_path)
    dirs: list[Path] = []
    for root in MEDIA_ROOTS:
        target = (root / clean).resolve() if clean else root
        if target.is_relative_to(root) and target.is_dir():
            dirs.append(target)
    if not dirs:
        raise HTTPException(status_code=404, detail="Directory not found")
    return dirs


def _resolve_file(rel_path: str) -> tuple[Path, Path]:
    clean = _safe_rel(rel_path)
    if not clean:
        raise HTTPException(status_code=404, detail="Not found")
    for root in MEDIA_ROOTS:
        target = (root / clean).resolve()
        if target.is_relative_to(root) and target.is_file():
            return root, target
    raise HTTPException(status_code=404, detail="Not found")


def _rel_from(root: Path, path: Path) -> str:
    if path == root:
        return ""
    return path.relative_to(root).as_posix()


def _url_path(rel_path: str) -> str:
    return urllib.parse.quote(rel_path, safe="/")


def _norm(name: str) -> str:
    """Normalize a filename stem for fuzzy cover-art matching."""
    s = name.lower()
    s = re.sub(r"\([^)]*\)", "", s)
    s = re.sub(r"\bposter\b", "", s)
    s = re.sub(r"\bpart\s*\d+\b", "", s)
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def _parse_nfo(nfo_path: Path) -> NfoMeta:
    meta = NfoMeta()
    if not nfo_path.is_file():
        return meta
    try:
        root = ET.parse(nfo_path).getroot()
    except ET.ParseError:
        return meta

    meta.title = (root.findtext("title") or "").strip()
    meta.year = (root.findtext("year") or "").strip()
    meta.plot = (root.findtext("plot") or "").strip()
    meta.director = (root.findtext("director") or "").strip()
    meta.season = (root.findtext("season") or "").strip()
    meta.episode = (root.findtext("episode") or "").strip()
    meta.genres = [g.text.strip() for g in root.findall("genre") if g.text and g.text.strip()]
    return meta


def _find_folder_art(folder: Path) -> Optional[Path]:
    try:
        entries = list(folder.iterdir())
    except OSError:
        return None

    images = [p for p in entries if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    if not images:
        return None

    videos = [p for p in entries if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
    video_stems = {p.stem.lower() for p in videos}
    video_norms = {_norm(p.stem) for p in videos}
    folder_norm = _norm(folder.name)

    for candidate in (folder.name.lower(), "poster", "folder", "cover", "fanart"):
        for img in images:
            if img.stem.lower() == candidate:
                return img

    for img in images:
        if _norm(img.stem) == folder_norm:
            return img

    for img in images:
        if img.stem.lower() not in video_stems and _norm(img.stem) not in video_norms:
            return img

    return None


def _collect_descendant_images(dirs: list[Path], limit: int = 4) -> list[tuple[Path, Path]]:
    """Collect up to `limit` images recursively from subdirectories (breadth-first)."""
    queue: list[tuple[Path, Path]] = []
    for d in dirs:
        root = next(r for r in MEDIA_ROOTS if d == r or d.is_relative_to(r))
        queue.append((root, d))

    found: list[tuple[Path, Path]] = []
    seen_rel: set[str] = set()
    idx = 0
    while idx < len(queue) and len(found) < limit:
        root, cur = queue[idx]
        idx += 1
        try:
            entries = sorted(cur.iterdir(), key=lambda p: (p.is_dir(), p.name.lower()))
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.is_file() and entry.suffix.lower() in IMAGE_EXTS:
                rel = _rel_from(root, entry)
                if rel not in seen_rel:
                    seen_rel.add(rel)
                    found.append((root, entry))
                    if len(found) >= limit:
                        break
            elif entry.is_dir():
                queue.append((root, entry))
    return found


def _find_video_art(root: Path, video: Path) -> Optional[Path]:
    parent = video.parent
    try:
        images = [p for p in parent.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    except OSError:
        return None

    stem_lower = video.stem.lower()
    for img in images:
        if img.stem.lower() == stem_lower:
            return img

    vid_norm = _norm(video.stem)
    for img in images:
        if _norm(img.stem) == vid_norm:
            return img

    if parent != root:
        return _find_folder_art(parent)
    return None


def _page_shell(title: str, breadcrumbs_html: str, body_html: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} — FanMedia</title>
  <style>
    :root {{
      --bg: #0f1115;
      --card: #181c24;
      --card-hover: #212631;
      --text: #e8ecf2;
      --muted: #9aa4b2;
      --accent: #e5a00d;
      --border: #272d3a;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }}
    header {{
      position: sticky;
      top: 0;
      z-index: 10;
      background: rgba(15, 17, 21, 0.92);
      backdrop-filter: blur(8px);
      border-bottom: 1px solid var(--border);
      padding: 0.85rem 1.5rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
      flex-wrap: wrap;
    }}
    .brand {{
      font-weight: 700;
      font-size: 1.1rem;
      color: var(--accent);
      text-decoration: none;
    }}
    .crumbs {{
      display: flex;
      align-items: center;
      gap: 0.4rem;
      flex-wrap: wrap;
      font-size: 0.92rem;
      color: var(--muted);
    }}
    .crumbs a {{ color: var(--text); text-decoration: none; }}
    .crumbs a:hover {{ color: var(--accent); }}
    main {{
      max-width: 1400px;
      margin: 0 auto;
      padding: 1.5rem;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(185px, 1fr));
      gap: 1.25rem;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      overflow: hidden;
      text-decoration: none;
      color: inherit;
      transition: transform 0.14s ease, background 0.14s ease, border-color 0.14s ease;
      display: flex;
      flex-direction: column;
    }}
    .card:hover {{
      transform: translateY(-3px);
      background: var(--card-hover);
      border-color: var(--accent);
    }}
    .poster {{
      aspect-ratio: 2 / 3;
      width: 100%;
      background: #12151c;
      display: flex;
      align-items: center;
      justify-content: center;
      overflow: hidden;
      position: relative;
    }}
    .poster img {{
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }}
    .collage {{
      display: grid;
      width: 100%;
      height: 100%;
      gap: 2px;
      background: var(--bg);
    }}
    .collage.n2 {{ grid-template-columns: 1fr 1fr; }}
    .collage.n3 {{ grid-template-columns: 1fr 1fr; grid-template-rows: 1fr 1fr; }}
    .collage.n3 img:first-child {{ grid-row: span 2; }}
    .collage.n4 {{ grid-template-columns: 1fr 1fr; grid-template-rows: 1fr 1fr; }}
    .placeholder {{
      padding: 1rem;
      text-align: center;
      color: var(--muted);
      font-size: 0.9rem;
    }}
    .badge {{
      position: absolute;
      top: 0.5rem;
      left: 0.5rem;
      background: rgba(15, 17, 21, 0.85);
      color: var(--accent);
      font-size: 0.72rem;
      font-weight: 700;
      padding: 0.15rem 0.45rem;
      border-radius: 4px;
      border: 1px solid var(--border);
    }}
    .meta {{
      padding: 0.7rem 0.8rem;
    }}
    .meta .title {{
      font-weight: 600;
      font-size: 0.92rem;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }}
    .meta .sub {{
      font-size: 0.78rem;
      color: var(--muted);
      margin-top: 0.25rem;
    }}
    .player-layout {{
      display: grid;
      grid-template-columns: minmax(0, 2fr) minmax(260px, 1fr);
      gap: 1.5rem;
      align-items: start;
    }}
    @media (max-width: 900px) {{
      .player-layout {{ grid-template-columns: 1fr; }}
    }}
    video {{
      width: 100%;
      max-height: 75vh;
      background: #000;
      border-radius: 10px;
      border: 1px solid var(--border);
    }}
    .info-panel {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 1.25rem;
    }}
    .info-panel h1 {{ margin: 0 0 0.4rem; font-size: 1.35rem; }}
    .pills {{
      display: flex;
      gap: 0.4rem;
      flex-wrap: wrap;
      margin: 0.6rem 0 1rem;
    }}
    .pill {{
      background: #232936;
      color: var(--muted);
      font-size: 0.78rem;
      padding: 0.2rem 0.55rem;
      border-radius: 999px;
    }}
    .actions {{
      display: flex;
      gap: 0.6rem;
      flex-wrap: wrap;
      margin-top: 1rem;
    }}
    .btn {{
      display: inline-block;
      background: var(--accent);
      color: #111;
      font-weight: 600;
      font-size: 0.88rem;
      padding: 0.5rem 0.9rem;
      border-radius: 6px;
      text-decoration: none;
    }}
    .btn.secondary {{
      background: #232936;
      color: var(--text);
      border: 1px solid var(--border);
    }}
    .notice {{
      background: #221d14;
      border: 1px solid #594418;
      color: #f3d078;
      padding: 1rem;
      border-radius: 8px;
      margin-bottom: 1rem;
    }}
  </style>
</head>
<body>
  <header>
    <div style="display:flex;align-items:center;gap:1rem;flex-wrap:wrap;">
      <a class="brand" href="/">FanMedia</a>
      <nav class="crumbs">{breadcrumbs_html}</nav>
    </div>
  </header>
  <main>
    {body_html}
  </main>
</body>
</html>"""


def _breadcrumbs(rel_path: str) -> str:
    parts = [p for p in rel_path.split("/") if p]
    links = ['<a href="/">Home</a>']
    acc = []
    for part in parts:
        acc.append(part)
        joined = "/".join(acc)
        links.append(f'<a href="/browse/{_url_path(joined)}">{html.escape(part)}</a>')
    return " / ".join(links)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return browse("")


@app.get("/browse/{rel_path:path}", response_class=HTMLResponse)
def browse(rel_path: str) -> HTMLResponse:
    clean = _safe_rel(rel_path)
    dirs = _resolve_dirs(clean)

    merged_dirs: dict[str, list[tuple[Path, Path]]] = {}
    videos: list[tuple[Path, Path]] = []

    for folder in dirs:
        root = next(r for r in MEDIA_ROOTS if folder == r or folder.is_relative_to(r))
        for entry in folder.iterdir():
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                merged_dirs.setdefault(entry.name, []).append((root, entry))
            elif entry.is_file() and entry.suffix.lower() in VIDEO_EXTS:
                videos.append((root, entry))

    cards: list[str] = []
    for name in sorted(merged_dirs, key=str.lower):
        dir_pairs = merged_dirs[name]
        root, entry = dir_pairs[0]
        art: Optional[Path] = None
        art_root = root
        for r, d in dir_pairs:
            candidate = _find_folder_art(d)
            if candidate is not None:
                art = candidate
                art_root = r
                break

        if art is not None:
            art_html = f'<img src="/art/{_url_path(_rel_from(art_root, art))}" alt="" loading="lazy">'
        else:
            thumbs = _collect_descendant_images([d for _, d in dir_pairs], limit=4)
            if len(thumbs) == 1:
                tr, tp = thumbs[0]
                art_html = f'<img src="/art/{_url_path(_rel_from(tr, tp))}" alt="" loading="lazy">'
            elif len(thumbs) >= 2:
                imgs = "".join(
                    f'<img src="/art/{_url_path(_rel_from(tr, tp))}" alt="" loading="lazy">'
                    for tr, tp in thumbs
                )
                art_html = f'<div class="collage n{len(thumbs)}">{imgs}</div>'
            else:
                art_html = f'<div class="placeholder">{html.escape(name)}</div>'

        nfo: Optional[NfoMeta] = None
        for _, d in dir_pairs:
            parsed = _parse_nfo(d / "tvshow.nfo")
            if not parsed.title:
                parsed = _parse_nfo(d / "folder.nfo")
            if parsed.title or parsed.plot or parsed.year:
                nfo = parsed
                break
        nfo = nfo or NfoMeta()
        title = nfo.title or name
        sub = nfo.year or "Folder"
        rel_entry = _rel_from(root, entry)
        cards.append(
            f'<a class="card" href="/browse/{_url_path(rel_entry)}">'
            f'<div class="poster">{art_html}<span class="badge">DIR</span></div>'
            f'<div class="meta"><div class="title">{html.escape(title)}</div>'
            f'<div class="sub">{html.escape(sub)}</div></div></a>'
        )

    for root, entry in sorted(videos, key=lambda pair: pair[1].name.lower()):
        art = _find_video_art(root, entry)
        art_html = (
            f'<img src="/art/{_url_path(_rel_from(root, art))}" alt="" loading="lazy">'
            if art
            else f'<div class="placeholder">{html.escape(entry.stem)}</div>'
        )
        nfo = _parse_nfo(entry.with_suffix(".nfo"))
        title = nfo.title or entry.stem
        sub_parts = [p for p in (nfo.year, nfo.director) if p]
        sub = " • ".join(sub_parts) if sub_parts else entry.suffix.lstrip(".").upper()
        rel_entry = _rel_from(root, entry)
        cards.append(
            f'<a class="card" href="/watch/{_url_path(rel_entry)}">'
            f'<div class="poster">{art_html}</div>'
            f'<div class="meta"><div class="title">{html.escape(title)}</div>'
            f'<div class="sub">{html.escape(sub)}</div></div></a>'
        )

    body = f'<div class="grid">{"".join(cards)}</div>' if cards else '<p>This folder is empty.</p>'
    page_title = Path(clean).name if clean else "Home"
    return HTMLResponse(_page_shell(page_title, _breadcrumbs(clean), body))


@app.get("/watch/{rel_path:path}", response_class=HTMLResponse)
def watch(rel_path: str) -> HTMLResponse:
    root, video = _resolve_file(rel_path)
    if video.suffix.lower() not in VIDEO_EXTS:
        raise HTTPException(status_code=404, detail="Video not found")

    clean = _rel_from(root, video)
    nfo = _parse_nfo(video.with_suffix(".nfo"))
    title = nfo.title or video.stem
    art = _find_video_art(root, video)
    poster_attr = f' poster="/art/{_url_path(_rel_from(root, art))}"' if art else ""
    stream_url = f"/stream/{_url_path(clean)}"

    if video.suffix.lower() in BROWSER_PLAYABLE_EXTS:
        player_html = f'<video controls preload="metadata"{poster_attr}><source src="{stream_url}"></video>'
    else:
        player_html = (
            '<div class="notice">This file format '
            f"({html.escape(video.suffix)}) isn't natively playable in most browsers. "
            "Use <strong>Open in VLC (.m3u)</strong> or <strong>Download</strong> on the right.</div>"
        )

    pills: list[str] = []
    if nfo.year:
        pills.append(f'<span class="pill">{html.escape(nfo.year)}</span>')
    if nfo.director:
        pills.append(f'<span class="pill">Editor: {html.escape(nfo.director)}</span>')
    for g in nfo.genres:
        pills.append(f'<span class="pill">{html.escape(g)}</span>')
    pills.append(f'<span class="pill">{html.escape(video.suffix.lstrip(".").upper())}</span>')

    plot_html = (
        f"<p>{html.escape(nfo.plot)}</p>"
        if nfo.plot
        else '<p style="color:var(--muted);">No description yet in .nfo file.</p>'
    )

    info_html = (
        f'<div class="info-panel"><h1>{html.escape(title)}</h1>'
        f'<div class="pills">{"".join(pills)}</div>'
        f"{plot_html}"
        '<div class="actions">'
        f'<a class="btn" href="/playlist/{_url_path(clean)}">Open in VLC (.m3u)</a>'
        f'<a class="btn secondary" href="{stream_url}" download>Download</a>'
        "</div></div>"
    )

    parent_rel = _rel_from(root, video.parent)
    body = f'<div class="player-layout"><div>{player_html}</div>{info_html}</div>'
    return HTMLResponse(_page_shell(title, _breadcrumbs(parent_rel), body))


@app.get("/stream/{rel_path:path}")
def stream(rel_path: str) -> FileResponse:
    _, video = _resolve_file(rel_path)
    if video.suffix.lower() not in VIDEO_EXTS:
        raise HTTPException(status_code=404, detail="Video not found")
    return FileResponse(video)


@app.get("/art/{rel_path:path}")
def art(rel_path: str) -> FileResponse:
    _, img = _resolve_file(rel_path)
    if img.suffix.lower() not in IMAGE_EXTS:
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(img)


@app.get("/playlist/{rel_path:path}")
def playlist(rel_path: str, request: Request) -> PlainTextResponse:
    _, video = _resolve_file(rel_path)
    if video.suffix.lower() not in VIDEO_EXTS:
        raise HTTPException(status_code=404, detail="Video not found")
    base = str(request.base_url).rstrip("/")
    m3u = f"#EXTM3U\n#EXTINF:-1,{video.stem}\n{base}/stream/{_url_path(rel_path)}\n"
    return PlainTextResponse(
        m3u,
        media_type="audio/x-mpegurl",
        headers={"Content-Disposition": f'attachment; filename="{video.stem}.m3u"'},
    )
