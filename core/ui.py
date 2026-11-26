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
import json
from html import escape
from typing import Optional

import aiohttp

from config import config

# Change these to your own details.
OWNER_USERNAME = "yutabothub"
UPDATES_URL = "https://t.me/X_Ankitsopurts"
SUPPORT_URL = "https://t.me/X_Ankitsopurts"
# Optional: a picture URL for the /start card. Empty means text only.
START_BANNER = os.environ.get("START_BANNER", "").strip()

_SMALL = dict(zip("abcdefghijklmnopqrstuvwxyz", "ᴀʙᴄᴅᴇғɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"))

HELP_SECTIONS = [
    (
        "Playback",
        [
            ("play, p", "song name or link, plays in voice chat"),
            ("vplay, vp", "same, but in video mode"),
            ("radio, stream", "live radio or stream link"),
            ("playlist, pl", "whole YouTube playlist"),
            ("skip, next", "play the next song"),
        ],
    ),
    (
        "Controls",
        [
            ("pause, ps", "pause the stream"),
            ("resume, rs", "resume the stream"),
            ("mute, m", "mute the voice chat"),
            ("unmute, um", "unmute the voice chat"),
            ("loop, repeat", "repeat the current song"),
            ("stop, leave", "leave voice chat and clear the queue"),
            ("mode, switch", "switch audio / video mode"),
        ],
    ),
    (
        "Queue and settings",
        [
            ("list, queue", "show the queue"),
            ("mix, shuffle", "shuffle the queue"),
            ("export, ep", "export the queue to a file"),
            ("import, ip", "import a queue file (reply to it)"),
            ("admins, adminsonly", "only admins can control"),
            ("lang, language", "set the group language"),
            ("ping", "check if the bot is alive"),
            ("repo", "source code"),
            ("restart, update", "restart the bot"),
        ],
    ),
]


def sc(text: str) -> str:
    """Write text in small capitals. Use it for labels only, never for commands."""
    return "".join(_SMALL.get(ch.lower(), ch) for ch in text)


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


class SentMessage:
    """Stands in for a Pyrogram message. The bot only needs .delete() on it."""

    def __init__(self, chat_id: int, message_id: int) -> None:
        self.chat_id = chat_id
        self.message_id = message_id

    async def delete(self) -> None:
        await call(
            "deleteMessage", {"chat_id": self.chat_id, "message_id": self.message_id}
        )


async def call(method: str, params: dict, photo_path: Optional[str] = None):
    """Call the Bot API directly. Raises RuntimeError if Telegram says no."""
    if not config.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/{method}"
    form = aiohttp.FormData()
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        elif isinstance(value, bool):
            value = "true" if value else "false"
        form.add_field(key, str(value))
    handle = None
    if photo_path:
        handle = open(photo_path, "rb")
        form.add_field("photo", handle, filename="cover.jpg", content_type="image/jpeg")
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, data=form) as response:
                data = await response.json(content_type=None)
    finally:
        if handle:
            handle.close()
    if not data.get("ok"):
        raise RuntimeError(f"{method} failed: {data.get('description')}")
    return data["result"]


def _without_style(params: dict) -> dict:
    markup = params.get("reply_markup")
    if not isinstance(markup, dict):
        return params
    rows = [
        [{k: v for k, v in button.items() if k != "style"} for button in row]
        for row in markup.get("inline_keyboard", [])
    ]
    return {**params, "reply_markup": {"inline_keyboard": rows}}


async def _send(method: str, params: dict, photo_path: Optional[str] = None):
    """Send with the button colours. If Telegram rejects them, send without."""
    try:
        return await call(method, params, photo_path)
    except RuntimeError:
        plain = _without_style(params)
        if plain == params:
            raise
        return await call(method, plain, photo_path)


def _button(text, style, callback=None, url=None) -> dict:
    # style is the button colour: "success" (green), "primary" (blue), "danger" (red).
    button = {"text": text, "style": style}
    if callback:
        button["callback_data"] = callback
    if url:
        button["url"] = url
    return button


def player_keyboard(chat_id: int) -> dict:
    def cb(action: str) -> str:
        return f"pl:{action}:{chat_id}"

    return {
        "inline_keyboard": [
            [
                _button("▶", "success", callback=cb("resume")),
                _button("⏸", "primary", callback=cb("pause")),
                _button("🔁", "primary", callback=cb("loop")),
                _button("⏭", "primary", callback=cb("skip")),
                _button("⏹", "danger", callback=cb("stop")),
            ],
            [
                _button("📜 " + sc("Queue"), "primary", callback=cb("queue")),
                _button("❌ " + sc("Close"), "danger", callback=cb("close")),
            ],
        ]
    }


def start_keyboard(bot_username: str, chat_id: int) -> dict:
    rows = []
    if bot_username:
        add_url = "https://t.me/" + bot_username + "?startgroup=true"
        rows.append([_button("➕ " + sc("Add me to your chat"), "success", url=add_url)])
    rows.append([_button("📖 " + sc("Help & commands"), "success", callback="help")])
    rows.append(
        [
            _button("📢 " + sc("Updates"), "primary", url=UPDATES_URL),
            _button("💬 " + sc("Support"), "primary", url=SUPPORT_URL),
        ]
    )
    rows.append([_button("❌ " + sc("Close"), "danger", callback=f"pl:close:{chat_id}")])
    return {"inline_keyboard": rows}


def user_link(user) -> str:
    if user is None:
        return ""
    name = escape(getattr(user, "first_name", None) or "user")
    return f'<a href="tg://user?id={user.id}">{name}</a>'


def now_playing_caption(
    title: str, page_url: str, duration: str, requester_html: str, chat_title: str = ""
) -> str:
    link = escape(page_url or "", quote=True)
    lines = [
        "▪ <b>" + sc("Melody") + '</b> : <a href="' + link + '">'
        + escape(_clip(title or "Unknown", 120)) + "</a>",
        "▪ <b>" + sc("Length") + "</b> : " + escape(duration or "N/A"),
        "▪ <b>" + sc("Stream type") + "</b> : " + sc("Audio"),
        "▪ <b>" + sc("Requested by") + "</b> : " + requester_html,
    ]
    head = (
        "<blockquote>" + sc("Playback activated.") + " | "
        + sc("Enjoy the music") + " |</blockquote>"
    )
    body = "<blockquote expandable>" + "\n".join(lines) + "</blockquote>"
    return head + "\n" + body


def start_caption(mention_html: str) -> str:
    parts = [
        "<blockquote>👋🏻 " + sc("Hello") + " " + mention_html + "</blockquote>",
        "<blockquote expandable>" + sc("This is telegram music player") + " 🎵",
        "▪ " + sc("Stream lives, radios, youtube videos and audio or video files in voice chats.") + ".",
        "▪ " + sc("Enjoy the cinematic view of music with your friends") + " 😉</blockquote>",
        "🧑‍💻 <b>" + sc("Made with") + " ❤️ " + sc("by") + " @" + OWNER_USERNAME + "!</b> 👑",
    ]
    return "\n".join(parts)


def help_html() -> str:
    prefix = config.PREFIXES[0] if config.PREFIXES else "/"
    parts = ["<b>" + sc("Available commands") + "</b> 🤖"]
    for title, commands in HELP_SECTIONS:
        rows = []
        for names, desc in commands:
            shown = " ".join(
                "<code>" + escape(prefix + name) + "</code>" for name in names.split(", ")
            )
            rows.append("▪ " + shown + " · " + escape(desc))
        parts.append(
            "<b>" + sc(title) + "</b>\n<blockquote expandable>"
            + "\n".join(rows) + "</blockquote>"
        )
    parts.append("© <b>Powered by: @" + OWNER_USERNAME + "</b>")
    return "\n".join(parts)


async def send_now_playing(
    chat_id: int, reply_to: Optional[int], caption: str, photo_path: Optional[str] = None
) -> SentMessage:
    params = {
        "chat_id": chat_id,
        "parse_mode": "HTML",
        "reply_markup": player_keyboard(chat_id),
    }
    if reply_to:
        params["reply_to_message_id"] = reply_to
        params["allow_sending_without_reply"] = True
    if photo_path:
        result = await _send("sendPhoto", {**params, "caption": caption}, photo_path)
    else:
        result = await _send(
            "sendMessage",
            {**params, "text": caption, "disable_web_page_preview": True},
        )
    return SentMessage(chat_id, result["message_id"])


async def send_start(chat_id: int, caption: str, bot_username: str) -> SentMessage:
    keyboard = start_keyboard(bot_username, chat_id)
    if START_BANNER:
        result = await _send(
            "sendPhoto",
            {
                "chat_id": chat_id,
                "photo": START_BANNER,
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": keyboard,
            },
        )
    else:
        result = await _send(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": caption,
                "parse_mode": "HTML",
                "reply_markup": keyboard,
                "disable_web_page_preview": True,
            },
        )
    return SentMessage(chat_id, result["message_id"])


async def send_help(chat_id: int) -> SentMessage:
    result = await _send(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": help_html(),
            "parse_mode": "HTML",
            "reply_markup": {
                "inline_keyboard": [
                    [_button("❌ " + sc("Close"), "danger", callback=f"pl:close:{chat_id}")]
                ]
            },
            "disable_web_page_preview": True,
        },
    )
    return SentMessage(chat_id, result["message_id"])
