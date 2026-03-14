"""MusicMe → Subsonic Bridge Server.

Translates Subsonic/OpenSubsonic REST API calls into MusicMe dataservice API calls.
Designed to be used with Music Assistant's OpenSubsonic provider.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any
from xml.etree.ElementTree import Element, SubElement, tostring

from aiohttp import web

# Tracks the current request's desired format (set by middleware)
_request_format: str = "xml"

from musicme_client import MusicMeClient

logger = logging.getLogger(__name__)

# --- Configuration from environment ---

MUSICME_EMAIL = os.environ.get("MUSICME_EMAIL", "")
MUSICME_PASSWORD = os.environ.get("MUSICME_PASSWORD", "")
SUBSONIC_USER = os.environ.get("SUBSONIC_USER", "musicme")
SUBSONIC_PASSWORD = os.environ.get("SUBSONIC_PASSWORD", "musicme")
PORT = int(os.environ.get("PORT", "4533"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

# Global MusicMe client (shared across requests)
client = MusicMeClient()


# ---------------------------------------------------------------------------
# Subsonic XML response helpers
# ---------------------------------------------------------------------------


def subsonic_response(status: str = "ok") -> Element:
    """Create a root <subsonic-response> element."""
    root = Element("subsonic-response")
    root.set("xmlns", "http://subsonic.org/restapi")
    root.set("status", status)
    root.set("version", "1.16.1")
    root.set("type", "MusicMe-Bridge")
    root.set("serverVersion", "0.1.0")
    root.set("openSubsonic", "true")
    return root


def error_response(code: int, message: str) -> web.Response:
    """Return a Subsonic error response."""
    root = subsonic_response("failed")
    err = SubElement(root, "error")
    err.set("code", str(code))
    err.set("message", message)
    return make_response(root)


def _element_to_dict(el: Element) -> dict[str, Any]:
    """Recursively convert an XML Element to a Subsonic JSON-compatible dict."""
    result: dict[str, Any] = {}
    result.update(el.attrib)
    children: dict[str, list[dict]] = {}
    for child in el:
        tag = child.tag
        child_dict = _element_to_dict(child)
        if child.text and child.text.strip():
            child_dict["value"] = child.text.strip()
        children.setdefault(tag, []).append(child_dict)
    for tag, items in children.items():
        if len(items) == 1 and tag not in (
            "artist", "album", "song", "child", "entry",
            "index", "playlist", "similarArtist",
        ):
            result[tag] = items[0]
        else:
            result[tag] = items
    return result


def make_response(root: Element) -> web.Response:
    """Serialize to XML or JSON depending on the request's 'f' parameter."""
    if _request_format == "json":
        data = {"subsonic-response": _element_to_dict(root)}
        return web.Response(
            body=json.dumps(data),
            content_type="application/json",
            charset="UTF-8",
        )
    body = b'<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(root, encoding="unicode").encode()
    return web.Response(body=body, content_type="text/xml", charset="UTF-8")


# ---------------------------------------------------------------------------
# Subsonic auth check
# ---------------------------------------------------------------------------


def check_auth(request: web.Request) -> bool:
    """Validate Subsonic authentication from query params."""
    params = request.query
    user = params.get("u", "")
    if user != SUBSONIC_USER:
        return False

    # Method 1: plain password (param "p")
    if p := params.get("p"):
        plain = p.removeprefix("enc:")
        if plain == SUBSONIC_PASSWORD:
            return True

    # Method 2: token + salt (Subsonic 1.13.0+)
    token = params.get("t", "")
    salt = params.get("s", "")
    if token and salt:
        expected = hashlib.md5((SUBSONIC_PASSWORD + salt).encode()).hexdigest()
        return token == expected

    return False


# ---------------------------------------------------------------------------
# Converters: MusicMe data → Subsonic XML elements
# ---------------------------------------------------------------------------

# MusicMe artist ID prefix to avoid clashes with album/track IDs
_ART_PREFIX = "ar-"
_RADIO_PREFIX = "rd-"


def _artist_to_xml(parent: Element, art: dict[str, Any], tag: str = "artist") -> Element:
    """Append a Subsonic <artist> element."""
    el = SubElement(parent, tag)
    aid = str(art.get("id", ""))
    el.set("id", f"{_ART_PREFIX}{aid}")
    el.set("name", art.get("name", "Unknown"))
    el.set("coverArt", f"{_ART_PREFIX}{aid}")
    el.set("albumCount", str(art.get("albumCount", 0)))
    return el


def _album_to_xml(parent: Element, alb: dict[str, Any], tag: str = "album") -> Element:
    """Append a Subsonic <album> / <child> element."""
    el = SubElement(parent, tag)
    barcode = str(alb.get("barcode", ""))
    el.set("id", barcode)
    el.set("name", alb.get("name", alb.get("title", "")))
    el.set("title", alb.get("name", alb.get("title", "")))
    el.set("isDir", "true")
    el.set("coverArt", barcode)
    el.set("songCount", str(alb.get("ntracks", 0)))
    el.set("duration", str(alb.get("duration", 0)))
    if alb.get("streetdate"):
        el.set("year", alb["streetdate"][:4])
    artists = alb.get("artists", [])
    if artists:
        el.set("artist", artists[0].get("name", ""))
        el.set("artistId", f"{_ART_PREFIX}{artists[0].get('id', '')}")
    if alb.get("cover_url"):
        el.set("coverArt", barcode)
    return el


def _track_to_xml(parent: Element, trk: dict[str, Any], tag: str = "song") -> Element:
    """Append a Subsonic <song> / <child> element."""
    el = SubElement(parent, tag)
    barcode = str(trk.get("barcode", ""))
    el.set("id", barcode)
    el.set("title", trk.get("title", ""))
    el.set("isDir", "false")
    el.set("duration", str(trk.get("duration", 0)))
    el.set("contentType", "audio/mp4")
    el.set("suffix", "mp4")
    el.set("type", "music")
    el.set("isVideo", "false")
    el.set("bitRate", "128")

    if "-" in barcode:
        album_barcode = barcode.split("-")[0]
        el.set("parent", album_barcode)
        el.set("albumId", album_barcode)
        el.set("coverArt", album_barcode)
    if "_" in barcode and "-" in barcode:
        try:
            disc_track = barcode.split("-", 1)[1]
            parts = disc_track.split("_")
            el.set("discNumber", parts[0])
            el.set("track", parts[1])
        except (IndexError, ValueError):
            pass

    artists = trk.get("artists", [])
    if artists:
        el.set("artist", ", ".join(a.get("name", "") for a in artists))
        el.set("artistId", f"{_ART_PREFIX}{artists[0].get('id', '')}")
    if trk.get("album"):
        el.set("album", trk["album"])
    if trk.get("cover_url"):
        el.set("coverArt", barcode.split("-")[0] if "-" in barcode else barcode)

    return el


# ---------------------------------------------------------------------------
# Subsonic API endpoint handlers
# ---------------------------------------------------------------------------


async def handle_ping(request: web.Request) -> web.Response:
    """Handle ping.view — connection test."""
    return make_response(subsonic_response())


async def handle_get_license(request: web.Request) -> web.Response:
    """Handle getLicense.view."""
    root = subsonic_response()
    lic = SubElement(root, "license")
    lic.set("valid", "true")
    lic.set("email", MUSICME_EMAIL)
    return make_response(root)


async def handle_get_open_subsonic_extensions(request: web.Request) -> web.Response:
    """Handle getOpenSubsonicExtensions.view."""
    root = subsonic_response()
    SubElement(root, "openSubsonicExtensions")
    return make_response(root)


async def handle_get_artists(request: web.Request) -> web.Response:
    """Handle getArtists.view — return top artists as the 'library'."""
    data = await client.get_tops()
    root = subsonic_response()
    artists_el = SubElement(root, "artists")

    if data:
        # Group by first letter
        by_letter: dict[str, list[dict]] = {}
        for art in data.get("results", {}).get("artists", []):
            if not art.get("id"):
                continue
            first = art.get("name", "?")[0].upper()
            if not first.isalpha():
                first = "#"
            by_letter.setdefault(first, []).append(art)

        for letter in sorted(by_letter.keys()):
            idx = SubElement(artists_el, "index")
            idx.set("name", letter)
            for art in by_letter[letter]:
                _artist_to_xml(idx, art)

    return make_response(root)


async def handle_get_artist(request: web.Request) -> web.Response:
    """Handle getArtist.view — artist details with albums."""
    artist_id = request.query.get("id", "").removeprefix(_ART_PREFIX)
    if not artist_id:
        return error_response(10, "Missing id parameter")

    data = await client.get_artist(artist_id)
    if not data or "item" not in data:
        return error_response(70, "Artist not found")

    root = subsonic_response()
    art_el = SubElement(root, "artist")
    item = data["item"]
    art_el.set("id", f"{_ART_PREFIX}{item.get('id', '')}")
    art_el.set("name", item.get("name", ""))
    art_el.set("coverArt", f"{_ART_PREFIX}{item.get('id', '')}")
    art_el.set("albumCount", str(len(data.get("results", {}).get("albums", []))))

    for alb in data.get("results", {}).get("albums", []):
        if alb.get("barcode"):
            _album_to_xml(art_el, alb)

    return make_response(root)


async def handle_get_artist_info2(request: web.Request) -> web.Response:
    """Handle getArtistInfo2.view."""
    artist_id = request.query.get("id", "").removeprefix(_ART_PREFIX)
    data = await client.get_artist(artist_id) if artist_id else None

    root = subsonic_response()
    info = SubElement(root, "artistInfo2")
    if data and "item" in data:
        item = data["item"]
        img = f"https://covers-ng3.hosting-media.net/art/r288/{item.get('id', '')}.jpg"
        SubElement(info, "largeImageUrl").text = img
        SubElement(info, "mediumImageUrl").text = img
        SubElement(info, "smallImageUrl").text = img

        for rel in data.get("results", {}).get("related-artists", [])[:10]:
            if rel.get("id"):
                sa = SubElement(info, "similarArtist")
                sa.set("id", f"{_ART_PREFIX}{rel['id']}")
                sa.set("name", rel.get("name", ""))

    return make_response(root)


async def handle_get_album(request: web.Request) -> web.Response:
    """Handle getAlbum.view — album with tracks."""
    album_id = request.query.get("id", "")
    if not album_id:
        return error_response(10, "Missing id parameter")

    data = await client.get_album(album_id)
    if not data or "item" not in data:
        return error_response(70, "Album not found")

    root = subsonic_response()
    item = data["item"]
    alb_el = _album_to_xml(root, item)

    for trk in data.get("results", {}).get("tracks", []):
        if trk.get("barcode") and trk.get("streamable", 0) == 2:
            child = _track_to_xml(alb_el, trk, tag="song")
            child.set("album", item.get("name", ""))
            child.set("parent", album_id)

    return make_response(root)


async def handle_get_album_info2(request: web.Request) -> web.Response:
    """Handle getAlbumInfo2.view."""
    album_id = request.query.get("id", "")
    data = await client.get_album(album_id) if album_id else None

    root = subsonic_response()
    info = SubElement(root, "albumInfo")
    if data and "item" in data:
        item = data["item"]
        if item.get("cover_url"):
            SubElement(info, "largeImageUrl").text = item["cover_url"]
    return make_response(root)


async def handle_get_album_list2(request: web.Request) -> web.Response:
    """Handle getAlbumList2.view — new releases, etc."""
    ltype = request.query.get("type", "newest")
    try:
        size = int(request.query.get("size", "20"))
    except ValueError:
        size = 20

    root = subsonic_response()
    al_el = SubElement(root, "albumList2")

    data = await client.get_news()
    if data:
        albums = data.get("results", {}).get("albums", [])
        for alb in albums[:size]:
            if alb.get("barcode") and alb.get("streamable", 0) == 2:
                _album_to_xml(al_el, alb)

    return make_response(root)


async def handle_get_song(request: web.Request) -> web.Response:
    """Handle getSong.view."""
    song_id = request.query.get("id", "")
    if not song_id or "-" not in song_id:
        return error_response(10, "Missing or invalid id parameter")

    album_barcode = song_id.split("-")[0]
    data = await client.get_album(album_barcode)
    if not data:
        return error_response(70, "Song not found")

    root = subsonic_response()
    for trk in data.get("results", {}).get("tracks", []):
        if trk.get("barcode") == song_id:
            _track_to_xml(root, trk)
            return make_response(root)

    return error_response(70, "Song not found")


async def handle_get_top_songs(request: web.Request) -> web.Response:
    """Handle getTopSongs.view."""
    artist_name = request.query.get("artist", "")
    try:
        count = int(request.query.get("count", "20"))
    except ValueError:
        count = 20

    root = subsonic_response()
    ts_el = SubElement(root, "topSongs")

    if not artist_name:
        return make_response(root)

    search_data = await client.search(artist_name, limit=1)
    if not search_data or "results" not in search_data:
        return make_response(root)

    artists = search_data["results"].get("artists", [])
    if not artists:
        return make_response(root)

    artist_id = str(artists[0].get("id", ""))
    art_data = await client.get_artist(artist_id, max_albums=5)
    if not art_data:
        return make_response(root)

    collected = 0
    for alb in art_data.get("results", {}).get("albums", []):
        if collected >= count:
            break
        barcode = alb.get("barcode")
        if not barcode or alb.get("streamable", 0) != 2:
            continue
        album_data = await client.get_album(barcode)
        if not album_data:
            continue
        for trk in album_data.get("results", {}).get("tracks", []):
            if collected >= count:
                break
            if trk.get("barcode") and trk.get("streamable", 0) == 2:
                _track_to_xml(ts_el, trk)
                collected += 1

    return make_response(root)


async def handle_search3(request: web.Request) -> web.Response:
    """Handle search3.view."""
    query = request.query.get("query", "")
    try:
        artist_count = int(request.query.get("artistCount", "10"))
        album_count = int(request.query.get("albumCount", "10"))
        song_count = int(request.query.get("songCount", "10"))
    except ValueError:
        artist_count = album_count = song_count = 10

    root = subsonic_response()
    sr = SubElement(root, "searchResult3")

    if not query:
        return make_response(root)

    limit = max(artist_count, album_count, song_count)
    data = await client.search(query, limit=limit)
    if not data or "results" not in data:
        return make_response(root)

    res = data["results"]
    for art in res.get("artists", [])[:artist_count]:
        if art.get("id"):
            _artist_to_xml(sr, art)
    for alb in res.get("albums", [])[:album_count]:
        if alb.get("barcode"):
            _album_to_xml(sr, alb)
    for trk in res.get("tracks", [])[:song_count]:
        if trk.get("barcode") and trk.get("streamable"):
            _track_to_xml(sr, trk)

    return make_response(root)


async def handle_get_playlists(request: web.Request) -> web.Response:
    """Handle getPlaylists.view — return radios as playlists."""
    root = subsonic_response()
    pls = SubElement(root, "playlists")

    data = await client.get_radios()
    if data:
        for item in data.get("results", {}).get("theme-airplays", []):
            radio_id = item.get("id", "")
            if not radio_id:
                continue
            pl = SubElement(pls, "playlist")
            pl.set("id", str(radio_id))
            pl.set("name", f"[Radio] {item.get('name', '')}")
            pl.set("songCount", "0")
            pl.set("public", "true")
            pl.set("owner", "MusicMe")
            if item.get("tile_url") or item.get("img_url"):
                pl.set("coverArt", str(radio_id))

    return make_response(root)


async def handle_get_playlist(request: web.Request) -> web.Response:
    """Handle getPlaylist.view — load radio tracks."""
    playlist_id = request.query.get("id", "")
    if not playlist_id:
        return error_response(10, "Missing id parameter")

    data = await client.get_airplay(playlist_id)
    if not data:
        return error_response(70, "Playlist not found")

    root = subsonic_response()
    pl = SubElement(root, "playlist")
    pl.set("id", playlist_id)
    item = data.get("item", {})
    pl.set("name", f"[Radio] {item.get('name', playlist_id)}")
    pl.set("public", "true")
    pl.set("owner", "MusicMe")

    tracks = data.get("results", {}).get("tracks", [])
    pl.set("songCount", str(len(tracks)))

    for trk in tracks:
        if trk.get("barcode") and trk.get("streamable", 0) == 2:
            _track_to_xml(pl, trk, tag="entry")

    return make_response(root)


async def handle_get_starred2(request: web.Request) -> web.Response:
    """Handle getStarred2.view — empty (no favorites API)."""
    root = subsonic_response()
    SubElement(root, "starred2")
    return make_response(root)


async def handle_star(request: web.Request) -> web.Response:
    """Handle star.view — no-op."""
    return make_response(subsonic_response())


async def handle_unstar(request: web.Request) -> web.Response:
    """Handle unstar.view — no-op."""
    return make_response(subsonic_response())


async def handle_get_cover_art(request: web.Request) -> web.Response:
    """Handle getCoverArt.view — proxy cover images from MusicMe CDN."""
    cover_id = request.query.get("id", "")
    if not cover_id:
        return web.Response(status=404)

    if cover_id.startswith(_ART_PREFIX):
        artist_id = cover_id.removeprefix(_ART_PREFIX)
        img_url = f"https://covers-ng3.hosting-media.net/art/r288/{artist_id}.jpg"
    elif cover_id.startswith("rd-") or cover_id.startswith("art-"):
        img_url = f"https://mmcdn-ng1.hosting-media.net/pict/radio/tile_radio_{cover_id.split('-', 1)[1]}.jpg"
    else:
        img_url = f"https://covers-ng2.hosting-media.net/jpg343/u{cover_id}.jpg"

    session = client._ensure_session()
    async with session.get(img_url) as resp:
        if resp.status != 200:
            return web.Response(status=404)
        data = await resp.read()
        ct = resp.headers.get("Content-Type", "image/jpeg")
        return web.Response(body=data, content_type=ct)


async def handle_stream(request: web.Request) -> web.Response:
    """Handle stream.view — get a fresh ticket and redirect to the stream URL."""
    song_id = request.query.get("id", "")
    if not song_id:
        return web.Response(status=400, text="Missing id")

    try:
        stream_url = await client.get_stream_url(song_id)
    except Exception as err:
        logger.error("Stream error for %s: %s", song_id, err)
        return web.Response(status=500, text=str(err))

    raise web.HTTPTemporaryRedirect(location=stream_url)


async def handle_get_similar_songs(request: web.Request) -> web.Response:
    """Handle getSimilarSongs.view — empty stub."""
    root = subsonic_response()
    SubElement(root, "similarSongs")
    return make_response(root)


async def handle_get_bookmarks(request: web.Request) -> web.Response:
    """Handle getBookmarks.view — empty stub."""
    root = subsonic_response()
    SubElement(root, "bookmarks")
    return make_response(root)


async def handle_create_bookmark(request: web.Request) -> web.Response:
    """Handle createBookmark.view — no-op stub."""
    return make_response(subsonic_response())


async def handle_delete_bookmark(request: web.Request) -> web.Response:
    """Handle deleteBookmark.view — no-op stub."""
    return make_response(subsonic_response())


async def handle_get_podcasts(request: web.Request) -> web.Response:
    """Handle getPodcasts.view — empty."""
    root = subsonic_response()
    SubElement(root, "podcasts")
    return make_response(root)


async def handle_get_newest_podcasts(request: web.Request) -> web.Response:
    """Handle getNewestPodcasts.view — empty."""
    root = subsonic_response()
    SubElement(root, "newestPodcasts")
    return make_response(root)


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------


@web.middleware
async def auth_middleware(request: web.Request, handler: Any) -> web.Response:
    """Check Subsonic authentication and detect response format on all /rest/ endpoints."""
    global _request_format  # noqa: PLW0603
    if request.path.startswith("/rest/"):
        _request_format = request.query.get("f", "xml")
        if not check_auth(request):
            return error_response(40, "Wrong username or password")
    return await handler(request)


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------


def create_app() -> web.Application:
    """Create the aiohttp web application with all Subsonic routes."""
    app = web.Application(middlewares=[auth_middleware])

    # Map all Subsonic endpoints (both GET and POST, as some clients use POST)
    endpoints = {
        "ping": handle_ping,
        "getLicense": handle_get_license,
        "getOpenSubsonicExtensions": handle_get_open_subsonic_extensions,
        "getArtists": handle_get_artists,
        "getArtist": handle_get_artist,
        "getArtistInfo2": handle_get_artist_info2,
        "getAlbum": handle_get_album,
        "getAlbumInfo2": handle_get_album_info2,
        "getAlbumList2": handle_get_album_list2,
        "getSong": handle_get_song,
        "getTopSongs": handle_get_top_songs,
        "search3": handle_search3,
        "getPlaylists": handle_get_playlists,
        "getPlaylist": handle_get_playlist,
        "getStarred2": handle_get_starred2,
        "star": handle_star,
        "unstar": handle_unstar,
        "getCoverArt": handle_get_cover_art,
        "stream": handle_stream,
        "getSimilarSongs": handle_get_similar_songs,
        "getBookmarks": handle_get_bookmarks,
        "createBookmark": handle_create_bookmark,
        "deleteBookmark": handle_delete_bookmark,
        "getPodcasts": handle_get_podcasts,
        "getNewestPodcasts": handle_get_newest_podcasts,
    }

    for name, handler in endpoints.items():
        app.router.add_get(f"/rest/{name}", handler)
        app.router.add_get(f"/rest/{name}.view", handler)
        app.router.add_post(f"/rest/{name}", handler)
        app.router.add_post(f"/rest/{name}.view", handler)

    return app


async def on_startup(app: web.Application) -> None:
    """Login to MusicMe on server start."""
    if not MUSICME_EMAIL or not MUSICME_PASSWORD:
        logger.error("MUSICME_EMAIL and MUSICME_PASSWORD must be set")
        raise SystemExit(1)
    await client.login(MUSICME_EMAIL, MUSICME_PASSWORD)
    logger.info("MusicMe-Subsonic-Bridge ready on port %d", PORT)
    logger.info("Subsonic user: %s", SUBSONIC_USER)


async def on_cleanup(app: web.Application) -> None:
    """Cleanup on shutdown."""
    await client.close()


def main() -> None:
    """Entry point."""
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    app = create_app()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, port=PORT, print=None)


if __name__ == "__main__":
    main()
