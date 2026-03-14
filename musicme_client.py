"""MusicMe API client — handles auth, encrypted API, and streaming tickets."""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

DATASERVICE_BASE = "https://dataservice.musicme.com/dataservice/v3"
STREAM_BASE = "https://stream.hosting-media.net/musicme"
WEB_BASE = "https://www.musicme.com"
PARTNER_ID = "22876"
_CLIENT_JSON = json.dumps(
    {"type": "desktop-web", "context": "www.musicme.com", "app": "mmplayer"}
)


def decrypt(crypted: str) -> str:
    """Decrypt a MusicMe API response or ticket string (XOR 0xAA cipher)."""
    if len(crypted) < 20:
        msg = f"Encrypted payload too short ({len(crypted)} chars)"
        raise ValueError(msg)

    crypted = crypted[10 : len(crypted) - 8]

    pos = -1
    for i in range(min(10, len(crypted))):
        c = crypted[i]
        if i % 2 == 1 and c in ("8", "B"):
            pos = i + 1
            break
    if pos == -1:
        pos = 10
    crypted = crypted[pos:]

    test = list(crypted)
    first_length = len(test) // 2 - (len(test) // 2) % 2
    sec_length = len(test) - first_length
    chars: list[str] = [""] * len(test)
    for j in range(first_length):
        chars[j + sec_length] = test[j]
    for j in range(sec_length):
        chars[j] = test[first_length + j]

    decrypted_chars: list[int] = []
    for i in range(len(chars) // 2):
        hex_str = chars[2 * i] + chars[2 * i + 1]
        b = int(hex_str, 16)
        decrypted_chars.append(b ^ 0xAA)

    return "".join(chr(b) for b in decrypted_chars)


class MusicMeClient:
    """Async client for MusicMe's dataservice API."""

    def __init__(self) -> None:
        self.session: aiohttp.ClientSession | None = None
        self.user_id: str | None = None
        self.catalog_size: str = "0"
        self._email: str = ""
        self._password: str = ""

    def _ensure_session(self) -> aiohttp.ClientSession:
        """Return the session, creating it if needed."""
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def close(self) -> None:
        """Close the HTTP session."""
        if self.session and not self.session.closed:
            await self.session.close()

    async def login(self, email: str, password: str) -> None:
        """Authenticate via web login and extract the userId."""
        self._email = email
        self._password = password
        session = self._ensure_session()

        async with session.post(
            f"{WEB_BASE}/mon-musicme/connexion/",
            data={"email": email, "password": password, "act": "login", "login_cookie": "1"},
            allow_redirects=True,
        ) as resp:
            resp.raise_for_status()

        async with session.get(f"{WEB_BASE}/?f=1") as resp:
            resp.raise_for_status()
            raw = await resp.read()
            content = raw.decode("latin-1", errors="replace")

        match = re.search(r"window\.playerInit\s*=\s*(\{.*?\});", content)
        if not match:
            raise RuntimeError("Login failed — could not find playerInit")

        player_init = json.loads(match.group(1))
        self.user_id = player_init.get("userId")
        self.catalog_size = player_init.get("catalogSize", "0")
        if not self.user_id:
            raise RuntimeError("Login failed — no userId in playerInit")

        logger.info("Logged in to MusicMe (catalog=%s)", self.catalog_size)

    async def _relogin(self) -> None:
        """Re-authenticate using stored credentials."""
        if not self._email or not self._password:
            raise RuntimeError("Cannot re-login: no stored credentials")
        logger.warning("Session expired, re-authenticating...")
        await self.login(self._email, self._password)

    def _base_params(self) -> str:
        parts = ["format=json", f"partnerid={PARTNER_ID}", f"client={_CLIENT_JSON}"]
        if self.user_id:
            parts.append(f"userid={self.user_id}")
        return "&".join(parts)

    async def api_get(self, endpoint: str, _retried: bool = False) -> dict[str, Any] | None:
        """GET a dataservice endpoint, decrypt and return JSON.

        Automatically re-authenticates once if the session appears expired.
        """
        session = self._ensure_session()
        url = f"{DATASERVICE_BASE}{endpoint}"
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{self._base_params()}"

        async with session.get(url) as resp:
            if resp.status in (404, 400):
                return None
            resp.raise_for_status()
            raw = await resp.text()

        try:
            return json.loads(decrypt(raw.strip()))
        except (ValueError, json.JSONDecodeError) as err:
            if not _retried:
                logger.warning("Decrypt failed (%s), attempting re-login", err)
                await self._relogin()
                return await self.api_get(endpoint, _retried=True)
            logger.warning("Failed to decrypt after re-login: %s", err)
            return None

    async def search(self, query: str, limit: int = 20) -> dict[str, Any] | None:
        """Search for artists, albums, tracks."""
        encoded = urllib.parse.quote_plus(query)
        return await self.api_get(
            f"/search?query={encoded}"
            f"&resources=artists{{maxResults:{limit}}}"
            f",albums{{maxResults:{limit}}}"
            f",tracks{{maxResults:{limit}}}"
        )

    async def get_artist(self, artist_id: str, max_albums: int = 50) -> dict[str, Any] | None:
        """Get artist with albums."""
        return await self.api_get(
            f"/artist/{artist_id}?resources=albums{{maxResults:{max_albums}}}"
            f",related-artists{{maxResults:10}}"
        )

    async def get_album(self, barcode: str) -> dict[str, Any] | None:
        """Get album with tracks."""
        return await self.api_get(f"/album/{barcode}?resources=tracks")

    async def get_radios(self) -> dict[str, Any] | None:
        """Get all radios."""
        return await self.api_get(
            "/radios?filters={theme:0}&resources=themes,theme-airplays{maxResults:100},home"
        )

    async def get_airplay(self, airplay_id: str) -> dict[str, Any] | None:
        """Get tracks for a radio/airplay."""
        return await self.api_get(f"/airplay/{airplay_id}?resources=tracks")

    async def get_stream_url(self, track_barcode: str) -> str:
        """Get a fresh ephemeral streaming URL for a track."""
        nocache = int(time.time() * 1000)
        data = await self.api_get(
            f"/getstream/{PARTNER_ID}?ref={track_barcode}&nocache={nocache}"
        )
        if not data or "ticket" not in data:
            raise RuntimeError(f"No streaming ticket for {track_barcode}")
        ticket = decrypt(data["ticket"])
        return f"{STREAM_BASE}/{PARTNER_ID}/{ticket}.mp4"

    async def get_tops(self) -> dict[str, Any] | None:
        """Get top artists."""
        return await self.api_get(
            "/tops?filters={style:0}&resources=styles,artists{maxResults:20},videos{maxResults:0}"
        )

    async def get_news(self, style_id: str = "0") -> dict[str, Any] | None:
        """Get new releases."""
        return await self.api_get(
            f"/news/{style_id}?filters={{style:{style_id}}}"
            f"&resources=albums{{maxResults:20}},focus-albums,styles"
        )
