import os
import re
import html
import time
import uuid
import asyncio
import logging
import threading

import yt_dlp
from flask import Flask
from telethon import TelegramClient, events, Button, errors
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeVideo, DocumentAttributeAudio

# --------------------------------------------------------------------------
# Configuration (Render -> Environment)
# --------------------------------------------------------------------------
def need(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} environment variable is not set.")
    return value


BOT_TOKEN = need("BOT_TOKEN")          # BotFather-er bot (apnar sathe kotha bolbe)
API_ID = int(need("API_ID"))           # my.telegram.org theke
API_HASH = need("API_HASH")            # my.telegram.org theke
SESSION_STRING = need("SESSION_STRING")  # make_session.py diye banano
CHANNEL_ID = need("CHANNEL_ID")        # @username ba -100... ID

OWNER_ID = int(os.environ.get("OWNER_ID", "1749461380"))
CAPTION = os.environ.get("CAPTION", "")
MAX_MB = int(os.environ.get("MAX_MB", "300"))
MAX_BYTES = MAX_MB * 1024 * 1024
MAX_LINKS = 100
PORT = int(os.environ.get("PORT", 10000))
DOWNLOAD_DIR = "downloads"

VIDEO_EXT = {".mp4", ".mov", ".m4v"}
AUDIO_EXT = {".mp3", ".m4a", ".ogg", ".opus", ".wav", ".flac", ".aac"}

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("large-link-bot")

URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

# Cloudflare anti-bot 403 er jonno: browser-er moto (Chrome) hoye request pathay.
# curl_cffi install thakle auto chalu hoy (requirements.txt e ache).
try:
    import curl_cffi  # noqa: F401
    from yt_dlp.networking.impersonate import ImpersonateTarget

    IMPERSONATE = ImpersonateTarget.from_str("chrome")
except Exception:  # noqa: BLE001
    IMPERSONATE = None
log.info("Browser impersonation: %s", "ON" if IMPERSONATE else "OFF")

# Telethon clients (main() e toiri hoy)
bot_client = None   # BotFather bot: apnar sathe kotha bole
user_client = None  # alada account: boro file channel e upload kore
channel = None
jobs = None
stop_flags = {}

# --------------------------------------------------------------------------
# Flask keep-alive server (Render)
# --------------------------------------------------------------------------
app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is alive!", 200


@app.route("/health")
def health():
    return {"status": "ok"}, 200


def run_flask():
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


def keep_alive():
    threading.Thread(target=run_flask, daemon=True).start()
    log.info("Keep-alive Flask server started on port %s", PORT)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def extract_urls(text: str) -> list:
    seen, urls = set(), []
    for raw in URL_PATTERN.findall(text or ""):
        url = raw.rstrip(".,;:!?)]}>")
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls[:MAX_LINKS]


def cleanup(file_id: str) -> None:
    """CLEANUP: server theke ei download-er shob file delete kore."""
    try:
        for name in os.listdir(DOWNLOAD_DIR):
            if name.startswith(file_id):
                path = os.path.join(DOWNLOAD_DIR, name)
                os.remove(path)
                log.info("Deleted %s", path)
    except OSError as exc:
        log.error("Cleanup error: %s", exc)


def short_error(exc: Exception) -> str:
    raw = str(exc).strip()
    text = raw.splitlines()[-1] if raw else "unknown"
    return html.escape(text.replace("ERROR: ", "")[:110])


# --------------------------------------------------------------------------
# Download (yt-dlp) + upload (user account, 2 GB porjonto)
# --------------------------------------------------------------------------
def download(url: str, file_id: str):
    options = {
        "outtmpl": os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s"),
        "format": "best[ext=mp4]/best",
        "noplaylist": True,
        "playlistend": 1,
        "max_filesize": MAX_BYTES,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
    }
    if IMPERSONATE is not None:
        options["impersonate"] = IMPERSONATE
    origin = re.match(r"https?://[^/]+", url)
    if origin:
        options["http_headers"] = {"Referer": origin.group(0) + "/"}
    if os.path.exists("cookies.txt"):
        options["cookiefile"] = "cookies.txt"

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        if info.get("_type") == "playlist" and info.get("entries"):
            info = info["entries"][0]
        path = ydl.prepare_filename(info)

    if not os.path.exists(path):
        for name in os.listdir(DOWNLOAD_DIR):
            if name.startswith(file_id) and not name.endswith(".part"):
                path = os.path.join(DOWNLOAD_DIR, name)
                break
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Video {MAX_MB} MB-er boro ba download kora file paoa gelo na."
        )
    return path, info


async def upload(path: str, info: dict, progress) -> None:
    ext = os.path.splitext(path)[1].lower()
    duration = int(info.get("duration") or 0)
    kw = {"caption": CAPTION or None, "progress_callback": progress}

    if ext in VIDEO_EXT:
        attributes = [
            DocumentAttributeVideo(
                duration=duration,
                w=int(info.get("width") or 0),
                h=int(info.get("height") or 0),
                supports_streaming=True,
            )
        ]
        await user_client.send_file(
            channel, path, attributes=attributes, supports_streaming=True, **kw
        )
    elif ext in AUDIO_EXT:
        attributes = [
            DocumentAttributeAudio(duration=duration, title=info.get("title"))
        ]
        await user_client.send_file(channel, path, attributes=attributes, **kw)
    else:
        await user_client.send_file(channel, path, force_document=True, **kw)


async def process_link(url: str, progress) -> None:
    file_id = uuid.uuid4().hex
    loop = asyncio.get_running_loop()
    try:
        path, info = await loop.run_in_executor(None, download, url, file_id)
        if os.path.getsize(path) > MAX_BYTES:
            raise ValueError(f"Video {MAX_MB} MB-er boro.")
        await upload(path, info, progress)
    finally:
        cleanup(file_id)


# --------------------------------------------------------------------------
# Job queue: ekta ekta kore link process hoy
# --------------------------------------------------------------------------
def stop_buttons(job_id: str):
    return [Button.inline("⏹ Bondho koren", data=f"stop:{job_id}".encode())]


async def edit_status(job: dict, text: str, with_stop: bool = True) -> None:
    try:
        await job["status"].edit(
            text, buttons=stop_buttons(job["id"]) if with_stop else None
        )
    except Exception as exc:  # noqa: BLE001
        if "not modified" not in str(exc).lower():
            log.warning("Status edit failed: %s", exc)


ABORT_ERRORS = (
    errors.ChatWriteForbiddenError,
    errors.ChatAdminRequiredError,
    errors.ChannelPrivateError,
)


async def run_job(job: dict) -> None:
    urls = job["urls"]
    stop = stop_flags[job["id"]]
    total = len(urls)
    ok, failed = 0, []
    aborted = None
    stopped = False

    for n, url in enumerate(urls, start=1):
        if stop.is_set():
            stopped = True
            break

        header = (
            f"⏳ {n}/{total} download hocche...\n"
            f"✅ {ok} ta upload hoyeche   ❌ {len(failed)} ta fail"
        )
        await edit_status(job, header)

        last = {"t": 0.0}

        async def progress(current, size, header=header, last=last):
            now = time.time()
            if size and now - last["t"] >= 8:
                last["t"] = now
                await edit_status(
                    job, f"{header}\n📤 Upload: {current * 100 // size}%"
                )

        try:
            await process_link(url, progress)
            ok += 1
        except yt_dlp.utils.DownloadError as exc:
            log.warning("Download failed for %s: %s", url, exc)
            failed.append((url, short_error(exc)))
        except ABORT_ERRORS as exc:
            log.error("Channel error: %s", exc)
            failed.append((url, "Channel e upload hoy nai"))
            aborted = (
                "Channel e post kora jacche na. Upload-er account ta channel-er "
                "Admin (Post Messages on) kina ar CHANNEL_ID thik kina dekhen."
            )
            break
        except Exception as exc:  # noqa: BLE001
            log.exception("Unexpected error for %s", url)
            failed.append((url, short_error(exc)))
        await asyncio.sleep(1)

    stop_flags.pop(job["id"], None)

    lines = [f"✅ <b>{ok}</b> ta video channel e upload hoyeche."]
    if failed:
        lines.append(f"❌ <b>{len(failed)}</b> ta fail hoyeche.")
    remaining = total - ok - len(failed)
    if stopped and remaining > 0:
        lines.append(f"⏹ Bondho kora holo, {remaining} ta baki chilo.")
    if aborted:
        lines.append(f"\n⚠️ {aborted}")

    try:
        await job["status"].delete()
    except Exception:  # noqa: BLE001
        pass
    await bot_client.send_message(job["chat_id"], "\n".join(lines))

    if failed:
        chunk = "<b>Fail hoya link gulo:</b>\n\n"
        for url, reason in failed:
            entry = f"{html.escape(url)}\n→ {reason}\n\n"
            if len(chunk) + len(entry) > 3500:
                await bot_client.send_message(
                    job["chat_id"], chunk, link_preview=False
                )
                chunk = ""
            chunk += entry
        if chunk:
            await bot_client.send_message(job["chat_id"], chunk, link_preview=False)


async def worker_loop():
    while True:
        job = await jobs.get()
        try:
            await run_job(job)
        except Exception:  # noqa: BLE001
            log.exception("Job failed")
        finally:
            jobs.task_done()


# --------------------------------------------------------------------------
# Bot handlers (BotFather bot)
# --------------------------------------------------------------------------
def register_handlers():
    @bot_client.on(events.NewMessage(incoming=True))
    async def on_message(event):
        if event.sender_id != OWNER_ID:
            await event.reply("⛔ Ei bot shudhu owner-er jonno.")
            return

        text = event.raw_text or ""
        if text.startswith("/"):
            await event.reply(
                "👋 <b>Welcome!</b>\n\n"
                "Je kono website-er video link pathan. Ami video download kore "
                "channel e upload kore dibo.\n\n"
                "• Ekbare onek link ek message e dite paren.\n"
                f"• Video {MAX_MB} MB porjonto hote pare.\n"
                "• Direct video/audio file link (.mp4, .mp3) o cholbe."
            )
            return

        urls = extract_urls(text)
        if not urls:
            await event.reply("❌ Video-r link pathan (http:// ba https:// diye).")
            return

        status = await event.reply(
            f"⏳ {len(urls)} ta link paoa gelo. Ekta ekta kore download hobe..."
        )
        job_id = uuid.uuid4().hex[:8]
        stop_flags[job_id] = asyncio.Event()
        await jobs.put(
            {
                "id": job_id,
                "chat_id": event.chat_id,
                "status": status,
                "urls": urls,
            }
        )

    @bot_client.on(events.CallbackQuery(pattern=b"stop:(.*)"))
    async def on_stop(event):
        if event.sender_id != OWNER_ID:
            await event.answer("⛔ Owner only", alert=True)
            return
        flag = stop_flags.get(event.pattern_match.group(1).decode())
        if flag:
            flag.set()
            await event.answer("⏹ Cholmaan link shesh hole bondho hobe")
        else:
            await event.answer("Ei kaj ta agei shesh hoye geche")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
async def resolve_channel():
    raw = CHANNEL_ID.strip()
    try:
        key = int(raw)
    except ValueError:
        key = raw
    # Account-er chat list load kore rakhi, private channel khuje pawar jonno
    await user_client.get_dialogs(limit=200)
    return await user_client.get_entity(key)


async def main():
    global bot_client, user_client, channel, jobs

    jobs = asyncio.Queue()

    bot_client = TelegramClient(StringSession(), API_ID, API_HASH)
    bot_client.parse_mode = "html"
    user_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

    await bot_client.start(bot_token=BOT_TOKEN)
    await user_client.connect()
    if not await user_client.is_user_authorized():
        raise RuntimeError(
            "SESSION_STRING kaj korche na. make_session.py abar chalie notun "
            "SESSION_STRING banan."
        )

    channel = await resolve_channel()
    register_handlers()
    asyncio.create_task(worker_loop())

    me = await user_client.get_me()
    log.info(
        "Bot is starting... Channel: %s | Owner: %s | Upload account: %s | Max: %s MB",
        CHANNEL_ID, OWNER_ID, me.id, MAX_MB,
    )
    await bot_client.run_until_disconnected()


if __name__ == "__main__":
    keep_alive()
    asyncio.run(main())
