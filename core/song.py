"""
Music Player, Telegram Voice Chat Bot
Copyright (c) 2021-present Asm Safone <https://github.com/AsmSafone>

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>
"""

import os
import re
import time
import shutil
import asyncio
from typing import Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse

from aiohttp import ClientSession
from pyrogram.types import User, Message
from yt_dlp import YoutubeDL

DOWNLOAD_DIR = os.path.abspath("downloads")
DOWNLOAD_MAX_AGE = 3 * 60 * 60  # seconds, older song folders are deleted
# Audio only. Progressive files are preferred because they download in one go.
AUDIO_FORMAT = "bestaudio[protocol!=m3u8_native][protocol!=m3u8]/bestaudio/best"
DEFAULT_THUMB = "https://telegra.ph/file/820cac7cb7b1a025542e2.jpg"
FETCH_TIMEOUT = 60  # seconds for one search + download
FIRST_TRY_TIMEOUT = 25  # seconds for the quick try on the top SoundCloud result
# YouTube is blocked on FadeHost. Set TRY_YOUTUBE=true only if your host can reach it.
TRY_YOUTUBE = os.environ.get("TRY_YOUTUBE", "false").lower() == "true"


def _format_duration(seconds) -> str:
    try:
        total = int(float(seconds or 0))
    except (TypeError, ValueError):
        return "N/A"
    if total <= 0:
        return "N/A"
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _cleanup_downloads() -> None:
    if not os.path.isdir(DOWNLOAD_DIR):
        return
    now = time.time()
    for name in os.listdir(DOWNLOAD_DIR):
        path = os.path.join(DOWNLOAD_DIR, name)
        try:
            if now - os.path.getmtime(path) > DOWNLOAD_MAX_AGE:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def _finished_files(folder: str) -> List[str]:
    if not os.path.isdir(folder):
        return []
    return [
        os.path.join(folder, name)
        for name in sorted(os.listdir(folder))
        if not name.endswith((".part", ".ytdl", ".temp"))
    ]


def _fetch(target: str, folder: str, same_title: Optional[str]):
    """Search or open `target` and download ONE song into `folder`.

    yt-dlp is blocking, so this runs in a worker thread. Returns
    (absolute file path, metadata dict) or (None, None).
    """
    os.makedirs(folder, exist_ok=True)

    def accept(info, *, incomplete=False):
        # Once one song is downloaded, skip the other search results.
        if _finished_files(folder):
            return "already have a song"
        if info.get("is_live"):
            return "live stream, not downloaded"
        title = (info.get("title") or "").lower()
        if same_title and title.startswith(same_title):
            return "same song as the last one"
        return None

    options = {
        "format": AUDIO_FORMAT,
        "outtmpl": os.path.join(folder, "%(id)s.%(ext)s"),
        "noplaylist": True,
        "playlist_items": "1-3",
        "ignoreerrors": True,
        "match_filter": accept,
        "concurrent_fragment_downloads": 16,
        "retries": 2,
        "fragment_retries": 3,
        "socket_timeout": 15,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
    }
    info = None
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(target, download=True)
    except Exception as e:
        print(f"[fetch] yt-dlp stopped for {target!r}: {type(e).__name__}: {str(e)[:300]}")

    files = _finished_files(folder)
    if not files:
        shutil.rmtree(folder, ignore_errors=True)
        return None, None
    path = files[0]
    track_id = os.path.splitext(os.path.basename(path))[0]
    if info and "entries" in info:
        entries = [e for e in (info.get("entries") or []) if e]
    else:
        entries = [info or {}]
    meta = next((e for e in entries if str(e.get("id")) == track_id), None)
    if meta is None:
        meta = entries[0] if entries else {}
    return os.path.abspath(path), meta


class Song:
    # Time until which YouTube is treated as blocked on this host.
    _yt_blocked_until: float = 0.0

    def __init__(self, link: Union[str, dict], request_msg: Message) -> None:
        self.title: Optional[str] = None      # markdown-safe, for text messages
        self.raw_title: Optional[str] = None  # plain, for the card and cover
        self.duration: str = "N/A"
        self.thumb: Optional[str] = None
        self.remote: Optional[str] = None
        self.headers: Optional[dict] = None
        self.protocol: str = ""
        self.page_url: Optional[str] = None
        self.source: str = link if isinstance(link, str) else ""
        self.parsed: bool = False
        self._retries: int = 0
        if isinstance(link, dict):
            self.parsed = True
            self.thumb = DEFAULT_THUMB
            for key, value in link.items():
                setattr(self, key, value)
        self.request_msg: Message = request_msg
        self.requested_by: User = request_msg.from_user

    async def parse(self) -> Tuple[bool, str]:
        if self.parsed:
            return (True, "ALREADY_PARSED")

        self._t0 = time.time()
        source = self.source
        is_search_text = "://" not in source
        is_youtube = "youtube.com" in source or "youtu.be" in source

        ok = False
        if is_search_text:
            # Plain song name: YouTube is blocked on this host, so search SoundCloud.
            # The top result is tried alone first (quickest); the top 3 only if it fails.
            query = self._clean_query(source)
            ok = await self._fetch_song(f"scsearch1:{query}", timeout=FIRST_TRY_TIMEOUT)
            if not ok:
                ok = await self._fetch_song(f"scsearch3:{query}")
        else:
            # By default a YouTube link goes straight to SoundCloud by its title,
            # because YouTube is blocked on this host and trying it costs seconds.
            youtube_skipped = is_youtube and (
                not TRY_YOUTUBE or time.time() < Song._yt_blocked_until
            )
            if not youtube_skipped:
                ok = await self._fetch_song(source)
                if not ok and is_youtube:
                    Song._yt_blocked_until = time.time() + 900
            # Fallback: search SoundCloud by the song name.
            if not ok and "soundcloud.com" not in source:
                query = await self._guess_title()
                if query:
                    query = self._clean_query(query)
                    print(f"[fallback] trying SoundCloud for {query!r}")
                    ok = await self._fetch_song(f"scsearch3:{query}", fallback=query)

        if ok:
            print(f"[song] ready in {time.time() - self._t0:.1f}s: {self.raw_title!r}")
            return (True, "PARSED")
        self._retries += 1
        return (False, "MAX_RETRY_LIMIT_REACHED")

    async def _fetch_song(
        self,
        target: str,
        same_title: Optional[str] = None,
        fallback: Optional[str] = None,
        timeout: float = FETCH_TIMEOUT,
    ) -> bool:
        _cleanup_downloads()
        folder = os.path.join(DOWNLOAD_DIR, str(time.time_ns()))
        try:
            path, meta = await asyncio.wait_for(
                asyncio.to_thread(_fetch, target, folder, same_title),
                timeout=timeout,
            )
        except Exception as e:
            print(f"[fetch] failed for {target!r}: {type(e).__name__}: {e}")
            return False
        if not path:
            return False
        self._apply(path, meta, fallback)
        return True

    def _apply(self, path: str, meta: dict, fallback: Optional[str]) -> None:
        self.raw_title = meta.get("title") or fallback or "Unknown"
        self.title = self._escape(self.raw_title)
        self.duration = _format_duration(meta.get("duration"))
        self.thumb = meta.get("thumbnail") or DEFAULT_THUMB
        self.remote = path  # local file, no network reads while playing
        self.headers = None
        self.protocol = "file"
        self.page_url = meta.get("webpage_url") or self.source
        self.parsed = True

    async def _guess_title(self) -> Optional[str]:
        """Find a song name when YouTube refused to give any details.

        1) The words the user typed after the command (/play gata only).
        2) For a pasted YouTube link, the title from YouTube's oEmbed page.
        """
        try:
            parts = (self.request_msg.text or "").split(maxsplit=1)
            if len(parts) > 1 and "://" not in parts[1]:
                return parts[1].strip()
        except BaseException:
            pass
        if "youtube.com" in self.source or "youtu.be" in self.source:
            session = ClientSession()
            try:
                response = await session.get(
                    "https://www.youtube.com/oembed",
                    params={"url": self.source, "format": "json"},
                    timeout=10,
                )
                if response.status == 200:
                    data = await response.json(content_type=None)
                    if data.get("title"):
                        return data["title"]
                print(f"[oembed] status {response.status}")
            except BaseException as e:
                print(f"[oembed] failed: {type(e).__name__}: {e}")
            finally:
                await session.close()
        return None

    @staticmethod
    def _clean_query(text: str) -> str:
        """Remove (Official Video), [4K Remaster] and similar from a title."""
        cleaned = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", text)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned or text

    @classmethod
    async def related(cls, last: "Song") -> Optional["Song"]:
        """Autoplay: download a song similar to the one that just ended."""
        base = (getattr(last, "raw_title", None) or last.title or "").replace("\\", "")
        base = cls._clean_query(base)
        if not base:
            return None
        song = cls(base, last.request_msg)
        ok = await song._fetch_song(
            f"scsearch8:{base}", same_title=base.lower()[:20], fallback=base
        )
        if not ok:
            print(f"[autoplay] nothing similar found for {base!r}")
            return None
        song.requested_by = last.requested_by
        song.source = song.page_url or base
        print(f"[autoplay] next: {song.raw_title!r}")
        return song

    @staticmethod
    async def check_remote_url(
        path: str, headers: Optional[Dict[str, str]] = None
    ) -> bool:
        host = urlparse(path).netloc
        session = ClientSession()
        try:
            response = await session.get(path, timeout=15, headers=headers)
            response.close()
            if response.status in (200, 206):
                return True
            print(f"[check] {host} answered with status {response.status}")
            return False
        except BaseException as e:
            print(f"[check] request to {host} failed: {type(e).__name__}: {e}")
            return False
        finally:
            await session.close()

    @staticmethod
    def _escape(_title: str) -> str:
        title = _title
        for i in ["**", "__", "`", "~~", "--"]:
            title = title.replace(i, f"\\{i}")
        return title

    def to_dict(self) -> Dict[str, str]:
        return {"title": self.title, "source": self.source}
