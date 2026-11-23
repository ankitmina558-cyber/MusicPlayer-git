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
import time
import shutil
import asyncio
from config import config
from core.song import Song
from core import ui
from core.stream import (
    forget_call, _SWITCHING, NOTICES, start_prefetch, cancel_prefetch, next_autoplay)
from lang import load
from pyrogram import enums
from pyrogram.types import Message, CallbackQuery
from pytgcalls import filters as fl
from pyrogram import Client, filters
from pytgcalls.types import Update, ChatUpdate
from pytgcalls.types.stream import StreamEnded
from core.decorators import language, register, only_admins, handle_error
from core.song import Song
from pytgcalls.exceptions import (
    NotInCallError, NoActiveGroupCall)
from core import (
    app, ytdl, safone, search, is_sudo, is_admin, get_group, get_queue,
    pytgcalls, set_group, set_title, all_groups, clear_queue, check_yt_url,
    extract_args, start_stream, shuffle_queue, delete_messages,
    get_spotify_playlist, get_youtube_playlist)


REPO = """
🤖 **Music Player**

- Repo: [GitHub](https://github.com/ankitmina558-cyber/MusicPlayer-git)
- License: AGPL-3.0-or-later
"""

# Autoplay: when the queue is empty, keep playing similar songs.
# Set environment variable AUTOPLAY=false to turn it off.
AUTOPLAY = os.environ.get("AUTOPLAY", "true").lower() == "true"

if config.BOT_TOKEN:
    bot = Client(
        "MusicPlayer",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.BOT_TOKEN,
        in_memory=True,
    )
    client = bot
else:
    client = app


@client.on_message(filters.command("repo", config.PREFIXES) & ~filters.bot)
@handle_error
async def repo(_, message: Message):
    await message.reply_text(REPO, disable_web_page_preview=True)


@client.on_message(filters.command("ping", config.PREFIXES) & ~filters.bot)
@handle_error
async def ping(_, message: Message):
    await message.reply_text(f"🤖 **Pong!**\n`{pytgcalls.ping} ms`")


@client.on_message(filters.command("start", config.PREFIXES) & ~filters.bot)
@language
@handle_error
async def start(_, message: Message, lang):
    try:
        bot_username = client.me.username if getattr(client, "me", None) else ""
        await ui.send_start(
            message.chat.id,
            ui.start_caption(ui.user_link(message.from_user)),
            bot_username or "",
        )
    except Exception as e:
        print(f"[start] styled card failed ({type(e).__name__}: {e}), sending plain text")
        await message.reply_text(lang["startText"] % message.from_user.mention)


@client.on_message(filters.command("help", config.PREFIXES) & ~filters.bot)
@language
@handle_error
async def help(_, message: Message, lang):
    try:
        await ui.send_help(message.chat.id)
    except Exception as e:
        print(f"[help] styled help failed ({type(e).__name__}: {e}), sending plain text")
        await message.reply_text(lang["helpText"].replace("<prefix>", config.PREFIXES[0]))


@client.on_message(filters.command(["p", "play", "vp", "vplay"], config.PREFIXES) & ~filters.private)
@register
@language
@handle_error
async def play_stream(_, message: Message, lang):
    # Reply at once, before any searching, so the user sees the bot respond.
    busy = await message.reply_text(lang["downloading"])
    try:
        return await _play_flow(message, lang, busy)
    finally:
        try:
            await busy.delete()
        except BaseException:
            pass


_BACKGROUND = set()


def _has_video(message) -> bool:
    """True for a video, a GIF animation or a video file."""
    if message is None:
        return False
    if any(getattr(message, n, None) for n in ("video", "animation", "video_note")):
        return True
    document = getattr(message, "document", None)
    return str(getattr(document, "mime_type", "") or "").startswith("video/")


def _delete_hint_later(message, seconds: int = 20) -> None:
    async def _later():
        await asyncio.sleep(seconds)
        try:
            await message.delete()
        except BaseException:
            pass

    task = asyncio.create_task(_later())
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


async def _find_song(message: Message):
    """Find the song for /play without the slow YouTube search.

    A plain name goes straight to SoundCloud (see core/song.py). Links, replied
    audio files and Spotify links keep the old handling in core/funcs.py.
    """
    reply = message.reply_to_message
    if reply and (reply.video_note or reply.animation):
        # search() in core/funcs.py only takes audio, video and document files.
        file = await reply.download()
        try:
            link = reply.link or ""
        except Exception:
            link = ""
        return Song({"title": "Video", "source": link, "remote": file}, message)
    if reply and not reply.text and reply.media:
        return await search(message)
    query = (reply.text if reply else extract_args(message.text)) or ""
    if query == "":
        return None
    is_yt_url, url = check_yt_url(query)
    if is_yt_url:
        return Song(url, message)
    if "open.spotify.com" in query:
        return await search(message)
    return Song(query, message)


async def _play_flow(message: Message, lang, busy):
    chat_id = message.chat.id
    group = get_group(chat_id)
    if group["admins_only"]:
        check = await is_admin(message)
        if not check:
            k = await message.reply_text(lang["notAllowed"])
            return await delete_messages([message, k])
    wants_video = message.command[0].lower() in ("vp", "vplay")
    song = await _find_song(message)
    if song is None:
        k = await message.reply_text(lang["notFound"])
        return await delete_messages([message, k])
    # /vplay shows video only when it replies to a video message. Any other
    # source plays as audio, and the chat is told so.
    song.video = wants_video and _has_video(message.reply_to_message)
    if wants_video and not song.video:
        _delete_hint_later(await message.reply_text(NOTICES["video_needs_reply"]))
    if not group["is_playing"]:
        # The voice chat is joined while the song downloads (see start_stream).
        set_group(chat_id, is_playing=True, now_playing=song)
        await start_stream(song, lang, busy)
        if AUTOPLAY and get_group(chat_id)["is_playing"]:
            start_prefetch(chat_id, song)
        await delete_messages([message])
    else:
        ok, status = await song.parse()
        if not ok:
            k = await message.reply_text(lang["notFound"])
            return await delete_messages([message, k])
        queue = get_queue(chat_id)
        await queue.put(song)
        k = await message.reply_text(
            lang["addedToQueue"] % (song.title, song.source, len(queue)),
            disable_web_page_preview=True,
        )
        await delete_messages([message, k])


@client.on_message(
    filters.command(["radio", "stream"], config.PREFIXES) & ~filters.private
)
@register
@language
@handle_error
async def live_stream(_, message: Message, lang):
    chat_id = message.chat.id
    group = get_group(chat_id)
    if group["admins_only"]:
        check = await is_admin(message)
        if not check:
            k = await message.reply_text(lang["notAllowed"])
            return await delete_messages([message, k])
    args = extract_args(message.text)
    if args is None:
        k = await message.reply_text(lang["notFound"])
        return await delete_messages([message, k])
    if " " in args and args.count(" ") == 1 and args[-5:] == "parse":
        song = Song({"source": args.split(" ")[0], "parsed": False}, message)
    else:
        is_yt_url, url = check_yt_url(args)
        if is_yt_url:
            meta = ytdl.extract_info(url, download=False)
            formats = meta.get("formats", [meta])
            for f in formats:
                ytstreamlink = f["url"]
            link = ytstreamlink
            song = Song(
                {"title": "YouTube Stream", "source": link, "remote": link}, message
            )
        else:
            song = Song(
                {"title": "Live Stream", "source": args, "remote": args}, message
            )
    ok, status = await song.parse()
    if not ok:
        k = await message.reply_text(lang["notFound"])
        return await delete_messages([message, k])
    if not group["is_playing"]:
        set_group(chat_id, is_playing=True, now_playing=song)
        await start_stream(song, lang)
        await delete_messages([message])
    else:
        queue = get_queue(chat_id)
        await queue.put(song)
        k = await message.reply_text(
            lang["addedToQueue"] % (song.title, song.source, len(queue)),
            disable_web_page_preview=True,
        )
        await delete_messages([message, k])


@client.on_message(
    filters.command(["skip", "next"], config.PREFIXES) & ~filters.private
)
@register
@language
@only_admins
@handle_error
async def skip_track(_, message: Message, lang):
    await _play_next(message.chat.id, lang, skipped=True)
    await delete_messages([message])


@client.on_message(filters.command(["m", "mute"], config.PREFIXES) & ~filters.private)
@register
@language
@only_admins
@handle_error
async def mute_vc(_, message: Message, lang):
    chat_id = message.chat.id
    try:
        await pytgcalls.mute_stream(chat_id)
        k = await message.reply_text(lang["muted"])
    except (NoActiveGroupCall, NotInCallError):
        k = await message.reply_text(lang["notActive"])
    await delete_messages([message, k])


@client.on_message(
    filters.command(["um", "unmute"], config.PREFIXES) & ~filters.private
)
@register
@language
@only_admins
@handle_error
async def unmute_vc(_, message: Message, lang):
    chat_id = message.chat.id
    try:
        await pytgcalls.unmute_stream(chat_id)
        k = await message.reply_text(lang["unmuted"])
    except (NoActiveGroupCall, NotInCallError):
        k = await message.reply_text(lang["notActive"])
    await delete_messages([message, k])


@client.on_message(filters.command(["ps", "pause"], config.PREFIXES) & ~filters.private)
@register
@language
@only_admins
@handle_error
async def pause_vc(_, message: Message, lang):
    chat_id = message.chat.id
    try:
        await pytgcalls.pause_stream(chat_id)
        k = await message.reply_text(lang["paused"])
    except (NoActiveGroupCall, NotInCallError):
        k = await message.reply_text(lang["notActive"])
    await delete_messages([message, k])


@client.on_message(
    filters.command(["rs", "resume"], config.PREFIXES) & ~filters.private
)
@register
@language
@only_admins
@handle_error
async def resume_vc(_, message: Message, lang):
    chat_id = message.chat.id
    try:
        await pytgcalls.resume_stream(chat_id)
        k = await message.reply_text(lang["resumed"])
    except (NoActiveGroupCall, NotInCallError):
        k = await message.reply_text(lang["notActive"])
    await delete_messages([message, k])


@client.on_message(
    filters.command(["stop", "leave"], config.PREFIXES) & ~filters.private
)
@register
@language
@only_admins
@handle_error
async def leave_vc(_, message: Message, lang):
    chat_id = message.chat.id
    set_group(chat_id, is_playing=False, now_playing=None)
    await set_title(message, "")
    clear_queue(chat_id)
    forget_call(chat_id)
    try:
        await pytgcalls.leave_call(chat_id)
        k = await message.reply_text(lang["leaveVC"])
    except (NoActiveGroupCall, NotInCallError):
        k = await message.reply_text(lang["notActive"])
    await delete_messages([message, k])


@client.on_message(
    filters.command(["list", "queue"], config.PREFIXES) & ~filters.private
)
@register
@language
@handle_error
async def queue_list(_, message: Message, lang):
    chat_id = message.chat.id
    queue = get_queue(chat_id)
    if len(queue) > 0:
        k = await message.reply_text(str(queue), disable_web_page_preview=True)
    else:
        k = await message.reply_text(lang["queueEmpty"])
    await delete_messages([message, k])


@client.on_message(
    filters.command(["mix", "shuffle"], config.PREFIXES) & ~filters.private
)
@register
@language
@only_admins
@handle_error
async def shuffle_list(_, message: Message, lang):
    chat_id = message.chat.id
    if len(get_queue(chat_id)) > 0:
        shuffled = shuffle_queue(chat_id)
        k = await message.reply_text(str(shuffled), disable_web_page_preview=True)
    else:
        k = await message.reply_text(lang["queueEmpty"])
    await delete_messages([message, k])


@client.on_message(
    filters.command(["loop", "repeat"], config.PREFIXES) & ~filters.private
)
@register
@language
@only_admins
@handle_error
async def loop_stream(_, message: Message, lang):
    chat_id = message.chat.id
    group = get_group(chat_id)
    if group["loop"]:
        set_group(chat_id, loop=False)
        k = await message.reply_text(lang["loopMode"] % "Disabled")
    elif group["loop"] == False:
        set_group(chat_id, loop=True)
        k = await message.reply_text(lang["loopMode"] % "Enabled")
    await delete_messages([message, k])


@client.on_message(
    filters.command(["mode", "switch"], config.PREFIXES) & ~filters.private
)
@register
@language
@only_admins
@handle_error
async def switch_mode(_, message: Message, lang):
    chat_id = message.chat.id
    group = get_group(chat_id)
    if group["stream_mode"] == "audio":
        set_group(chat_id, stream_mode="video")
        k = await message.reply_text(lang["videoMode"])
    else:
        set_group(chat_id, stream_mode="audio")
        k = await message.reply_text(lang["audioMode"])
    await delete_messages([message, k])


@client.on_message(
    filters.command(["admins", "adminsonly"], config.PREFIXES) & ~filters.private
)
@register
@language
@only_admins
@handle_error
async def admins_only(_, message: Message, lang):
    chat_id = message.chat.id
    group = get_group(chat_id)
    if group["admins_only"]:
        set_group(chat_id, admins_only=False)
        k = await message.reply_text(lang["adminsOnly"] % "Disabled")
    else:
        set_group(chat_id, admins_only=True)
        k = await message.reply_text(lang["adminsOnly"] % "Enabled")
    await delete_messages([message, k])


@client.on_message(
    filters.command(["lang", "language"], config.PREFIXES) & ~filters.private
)
@register
@language
@only_admins
@handle_error
async def set_lang(_, message: Message, lang):
    chat_id = message.chat.id
    lng = extract_args(message.text)
    if lng != "":
        langs = [
            file.replace(".json", "")
            for file in os.listdir(f"{os.getcwd()}/lang/")
            if file.endswith(".json")
        ]
        if lng == "list":
            k = await message.reply_text("\n".join(langs))
        elif lng in langs:
            set_group(chat_id, lang=lng)
            k = await message.reply_text(lang["langSet"] % lng)
        else:
            k = await message.reply_text(lang["notFound"])
        await delete_messages([message, k])


@client.on_message(
    filters.command(["ep", "export"], config.PREFIXES) & ~filters.private
)
@register
@language
@only_admins
@handle_error
async def export_queue(_, message: Message, lang):
    chat_id = message.chat.id
    queue = get_queue(chat_id)
    if len(queue) > 0:
        data = json.dumps([song.to_dict() for song in queue], indent=2)
        filename = f"{message.chat.username or message.chat.id}.json"
        with open(filename, "w") as file:
            file.write(data)
        await message.reply_document(
            filename, caption=lang["queueExported"] % len(queue)
        )
        os.remove(filename)
        await delete_messages([message])
    else:
        k = await message.reply_text(lang["queueEmpty"])
        await delete_messages([message, k])


@client.on_message(
    filters.command(["ip", "import"], config.PREFIXES) & ~filters.private
)
@register
@language
@only_admins
@handle_error
async def import_queue(_, message: Message, lang):
    if not message.reply_to_message or not message.reply_to_message.document:
        k = await message.reply_text(lang["replyToAFile"])
        return await delete_messages([message, k])
    chat_id = message.chat.id
    filename = await message.reply_to_message.download()
    data_str = None
    with open(filename, "r") as file:
        data_str = file.read()
    try:
        data = json.loads(data_str)
    except json.JSONDecodeError:
        k = await message.reply_text(lang["invalidFile"])
        return await delete_messages([message, k])
    try:
        temp_queue = []
        for song_dict in data:
            song = Song(song_dict["source"], message)
            song.title = song_dict["title"]
            temp_queue.append(song)
    except BaseException:
        k = await message.reply_text(lang["invalidFile"])
        return await delete_messages([message, k])
    group = get_group(chat_id)
    queue = get_queue(chat_id)
    if group["is_playing"]:
        for _song in temp_queue:
            await queue.put(_song)
    else:
        song = temp_queue[0]
        set_group(chat_id, is_playing=True, now_playing=song)
        await start_stream(song, lang)
        for _song in temp_queue[1:]:
            await queue.put(_song)
    k = await message.reply_text(lang["queueImported"] % len(temp_queue))
    await delete_messages([message, k])


@client.on_message(
    filters.command(["pl", "playlist"], config.PREFIXES) & ~filters.private
)
@register
@language
@handle_error
async def import_playlist(_, message: Message, lang):
    chat_id = message.chat.id
    group = get_group(chat_id)
    if group["admins_only"]:
        check = await is_admin(message)
        if not check:
            k = await message.reply_text(lang["notAllowed"])
            return await delete_messages([message, k])
    if message.reply_to_message:
        text = message.reply_to_message.text
    else:
        text = extract_args(message.text)
    if text == "":
        k = await message.reply_text(lang["notFound"])
        return await delete_messages([message, k])
    if "youtube.com/playlist?list=" in text:
        try:
            temp_queue = get_youtube_playlist(text, message)
        except BaseException:
            k = await message.reply_text(lang["notFound"])
            return await delete_messages([message, k])
    elif "open.spotify.com/playlist/" in text:
        if not config.SPOTIFY:
            k = await message.reply_text(lang["spotifyNotEnabled"])
            return await delete_messages([message, k])
        try:
            temp_queue = get_spotify_playlist(text, message)
        except BaseException:
            k = await message.reply_text(lang["notFound"])
            return await delete_messages([message, k])
    else:
        k = await message.reply_text(lang["invalidFile"])
        return await delete_messages([message, k])
    queue = get_queue(chat_id)
    if not group["is_playing"]:
        try:
            song = await temp_queue.__anext__()
        except StopAsyncIteration:
            k = await message.reply_text(lang["notFound"])
            return await delete_messages([message, k])
        set_group(chat_id, is_playing=True, now_playing=song)
        await start_stream(song, lang)
        async for _song in temp_queue:
            await queue.put(_song)
    else:
        async for _song in temp_queue:
            await queue.put(_song)
    k = await message.reply_text(lang["queueImported"] % len(group["queue"]))
    await delete_messages([message, k])


@client.on_message(
    filters.command(["update", "restart"], config.PREFIXES) & ~filters.private
)
@language
@handle_error
async def update_restart(_, message: Message, lang):
    check = await is_sudo(message)
    if not check:
        k = await message.reply_text(lang["notAllowed"])
        return await delete_messages([message, k])
    chats = all_groups()
    stats = await message.reply_text(lang["update"])
    for chat in chats:
        try:
            await pytgcalls.leave_call(chat)
        except (NoActiveGroupCall, NotInCallError):
            pass
    await stats.edit_text(lang["restart"])
    shutil.rmtree("downloads", ignore_errors=True)
    os.system(f"kill -9 {os.getpid()} && bash startup.sh")


async def _play_next(chat_id: int, lang, skipped: bool = False) -> None:
    """Play the next song: repeat, the queue, autoplay, or leave the call.

    A skip moves on even when loop is on. A song that ends repeats."""
    group = get_group(chat_id)
    if group["loop"] and not skipped and group["now_playing"] is not None:
        await start_stream(group["now_playing"], lang)
        return
    queue = get_queue(chat_id)
    if len(queue) > 0:
        cancel_prefetch(chat_id)
        next_song = await queue.get()
        set_group(chat_id, now_playing=next_song)
        await start_stream(next_song, lang)
        if AUTOPLAY and len(queue) == 0 and get_group(chat_id)["is_playing"]:
            start_prefetch(chat_id, next_song)
        return
    last = group.get("now_playing")
    next_song = None
    if AUTOPLAY and isinstance(last, Song):
        try:
            next_song = await next_autoplay(chat_id, last)
        except Exception:
            next_song = None
    if next_song is not None:
        set_group(chat_id, now_playing=next_song)
        await start_stream(next_song, lang)
        if get_group(chat_id)["is_playing"]:
            start_prefetch(chat_id, next_song)
        return
    # The song card stays in the chat. Only the close button removes it.
    await set_title(chat_id, "", client=app)
    set_group(chat_id, is_playing=False, now_playing=None)
    forget_call(chat_id)
    try:
        await pytgcalls.leave_call(chat_id)
    except (NoActiveGroupCall, NotInCallError):
        pass


def _lang_for(chat_id: int):
    try:
        return load(get_group(chat_id)["lang"])
    except Exception:
        return load(config.LANGUAGE)


async def _can_control(query: CallbackQuery, chat_id: int) -> bool:
    user = query.from_user
    if user is None:
        return False
    if user.id in config.SUDOERS:
        return True
    try:
        member = await client.get_chat_member(chat_id, user.id)
    except Exception:
        return False
    return member.status in (
        enums.ChatMemberStatus.ADMINISTRATOR,
        enums.ChatMemberStatus.OWNER,
    )


async def _answer(query: CallbackQuery, text: str = "", alert: bool = False) -> None:
    try:
        await query.answer(text, show_alert=alert)
    except Exception:
        pass


@client.on_callback_query(filters.regex(r"^pl:[a-z]+:-?\d+$"))
async def player_buttons(_, query: CallbackQuery):
    _, action, raw_id = query.data.split(":")
    chat_id = int(raw_id)
    lang = _lang_for(chat_id)
    try:
        if action == "close":
            await _answer(query)
            await query.message.delete()
            return
        if action == "queue":
            queue = get_queue(chat_id)
            text = str(queue) if len(queue) > 0 else lang["queueEmpty"]
            await _answer(query)
            await client.send_message(chat_id, text, disable_web_page_preview=True)
            return
        if not await _can_control(query, chat_id):
            await _answer(query, lang["notAllowed"], alert=True)
            return
        if action == "pause":
            await pytgcalls.pause_stream(chat_id)
            await _answer(query, "⏸ Paused")
        elif action == "resume":
            await pytgcalls.resume_stream(chat_id)
            await _answer(query, "▶ Resumed")
        elif action == "loop":
            enabled = not get_group(chat_id)["loop"]
            set_group(chat_id, loop=enabled)
            await _answer(query, lang["loopMode"] % ("Enabled" if enabled else "Disabled"))
        elif action == "skip":
            await _answer(query, "⏭ Skipping")
            await _play_next(chat_id, lang, skipped=True)
        elif action == "stop":
            set_group(chat_id, is_playing=False, now_playing=None)
            clear_queue(chat_id)
            await set_title(chat_id, "", client=app)
            await _answer(query, "⏹ Stopped")
            forget_call(chat_id)
            await pytgcalls.leave_call(chat_id)
        else:
            await _answer(query)
    except (NoActiveGroupCall, NotInCallError):
        await _answer(query, lang["notActive"], alert=True)
    except Exception as e:
        print(f"[button] {action} failed: {type(e).__name__}: {e}")
        await _answer(query, "Something went wrong, try again.", alert=True)


@client.on_callback_query(filters.regex(r"^help$"))
async def help_button(_, query: CallbackQuery):
    await _answer(query)
    try:
        await ui.send_help(query.message.chat.id)
    except Exception as e:
        print(f"[help] could not send help: {type(e).__name__}: {e}")


@pytgcalls.on_update()
@language
@handle_error
async def stream_end(_, update: Update, lang):
    if not isinstance(update, StreamEnded):
        return
    chat_id = update.chat_id
    group = get_group(chat_id)
    # Ignore an end that is not a real song end: no song is playing, the stream
    # is being switched right now, or the song only just started (the silence
    # or the old stream being replaced can report an end at that moment).
    if not group["is_playing"] or chat_id in _SWITCHING:
        return
    if time.time() - (group.get("stream_started_at") or 0) < 2:
        return
    await _play_next(chat_id, lang)


@pytgcalls.on_update(fl.chat_update(ChatUpdate.Status.LEFT_CALL))
@handle_error
async def closed_vc(_, update: Update):
    chat_id = update.chat_id
    # The music account is out of this voice chat, however it ended.
    forget_call(chat_id)
    if chat_id in all_groups():
        await set_title(chat_id, "", client=app)
        set_group(chat_id, now_playing=None, is_playing=False)
        clear_queue(chat_id)


client.start()
pytgcalls.run()
