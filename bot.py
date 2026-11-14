import os
import re
import html
import time
import uuid
import queue
import logging
import threading

import telebot
import yt_dlp
from flask import Flask

# --------------------------------------------------------------------------
# Configuration (Render -> Environment)
# --------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set.")

# Je channel e video upload hobe (@username ba -100... ID)
CHANNEL_ID = os.environ.get("CHANNEL_ID")
if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID environment variable is not set.")

# Shudhu ei user bot use korte parbe (apnar Telegram user ID)
OWNER_ID = int(os.environ.get("OWNER_ID", "1749461380"))

# Optional: prottek post-er caption (khali rakhle caption jabe na)
CAPTION = os.environ.get("CAPTION", "")

PORT = int(os.environ.get("PORT", 10000))
DOWNLOAD_DIR = "downloads"
MAX_FILE_SIZE = 50 * 1024 * 1024  # Telegram Bot API upload limit: 50 MB
MAX_LINKS = 100

VIDEO_EXT = {".mp4", ".mov", ".m4v"}
AUDIO_EXT = {".mp3", ".m4a", ".ogg", ".opus", ".wav", ".flac", ".aac"}

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("link-video-bot")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True)

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
    thread = threading.Thread(target=run_flask, daemon=True)
    thread.start()
    log.info("Keep-alive Flask server started on port %s", PORT)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def extract_urls(text: str) -> list:
    """Message theke shob link ber kore (duplicate bad diye, order thik rekhe)."""
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


def tg_retry(func):
    """Telegram flood limit (429) hole opekkha kore abar try kore."""
    for _ in range(5):
        try:
            return func()
        except telebot.apihelper.ApiTelegramException as exc:
            if exc.error_code != 429:
                raise
            wait = 5
            try:
                wait = int(exc.result_json["parameters"]["retry_after"]) + 1
            except Exception:  # noqa: BLE001
                pass
            log.warning("Flood limit, waiting %s s", wait)
            time.sleep(wait)
    raise RuntimeError("Telegram rate limit")


def short_error(exc: Exception) -> str:
    text = str(exc).strip().splitlines()[-1] if str(exc).strip() else "unknown"
    text = text.replace("ERROR: ", "")
    return html.escape(text[:110])


# --------------------------------------------------------------------------
# Download + upload
# --------------------------------------------------------------------------
def download(url: str, file_id: str):
    """yt-dlp diye download kore (path, info) return kore."""
    options = {
        "outtmpl": os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s"),
        "format": "best[ext=mp4][filesize<50M]/best[ext=mp4]/best[filesize<50M]/best",
        "noplaylist": True,
        "playlistend": 1,
        "max_filesize": MAX_FILE_SIZE,
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
        raise FileNotFoundError("Download kora file khuje paoa gelo na.")
    return path, info


def send_to_channel(path: str, info: dict) -> None:
    ext = os.path.splitext(path)[1].lower()
    kw = {"caption": CAPTION, "parse_mode": ""} if CAPTION else {}

    def do_send():
        with open(path, "rb") as f:
            if ext in VIDEO_EXT:
                duration = info.get("duration")
                bot.send_video(
                    CHANNEL_ID,
                    f,
                    supports_streaming=True,
                    duration=int(duration) if duration else None,
                    width=info.get("width"),
                    height=info.get("height"),
                    timeout=300,
                    **kw,
                )
            elif ext in AUDIO_EXT:
                bot.send_audio(
                    CHANNEL_ID, f, title=info.get("title"), timeout=300, **kw
                )
            else:
                bot.send_document(CHANNEL_ID, f, timeout=300, **kw)

    tg_retry(do_send)


def process_link(url: str) -> None:
    """Ekta link download kore channel e pathay, file delete kore."""
    file_id = uuid.uuid4().hex
    try:
        path, info = download(url, file_id)
        if os.path.getsize(path) > MAX_FILE_SIZE:
            raise ValueError("Video 50 MB-er boro, Telegram bot eta pathate pare na.")
        send_to_channel(path, info)
    finally:
        cleanup(file_id)


# --------------------------------------------------------------------------
# Job queue: ekta ekta kore link process hoy
# --------------------------------------------------------------------------
jobs = queue.Queue()
stop_flags = {}


def stop_markup(job_id: str):
    kb = telebot.types.InlineKeyboardMarkup()
    kb.row(
        telebot.types.InlineKeyboardButton(
            "⏹ Bondho koren", callback_data=f"stop:{job_id}"
        )
    )
    return kb


def edit_status(job: dict, text: str, markup=None) -> None:
    try:
        bot.edit_message_text(
            text,
            chat_id=job["chat_id"],
            message_id=job["status_id"],
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    except Exception as exc:  # noqa: BLE001
        if "not modified" not in str(exc):
            log.warning("Status edit failed: %s", exc)


def run_job(job: dict) -> None:
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

        edit_status(
            job,
            f"⏳ {n}/{total} download hocche...\n"
            f"✅ {ok} ta upload hoyeche   ❌ {len(failed)} ta fail",
            stop_markup(job["id"]),
        )
        try:
            process_link(url)
            ok += 1
        except yt_dlp.utils.DownloadError as exc:
            log.warning("Download failed for %s: %s", url, exc)
            failed.append((url, short_error(exc)))
        except telebot.apihelper.ApiTelegramException as exc:
            log.error("Telegram error for %s: %s", url, exc)
            failed.append((url, "Channel e upload hoy nai"))
            if exc.error_code in (400, 403):
                aborted = (
                    "Channel e post kora jacche na. Bot ta channel-er Admin "
                    "(Post Messages on) kina ar CHANNEL_ID thik kina dekhen."
                )
                break
        except Exception as exc:  # noqa: BLE001
            log.exception("Unexpected error for %s", url)
            failed.append((url, short_error(exc)))
        time.sleep(1)

    stop_flags.pop(job["id"], None)

    lines = [f"✅ <b>{ok}</b> ta video channel e upload hoyeche."]
    if failed:
        lines.append(f"❌ <b>{len(failed)}</b> ta fail hoyeche.")
    remaining = total - ok - len(failed)
    if stopped and remaining > 0:
        lines.append(f"⏹ Bondho kora holo, {remaining} ta baki chilo.")
    if aborted:
        lines.append(f"\n⚠️ {aborted}")
    edit_status(job, "\n".join(lines))

    if failed:
        chunk = "<b>Fail hoya link gulo:</b>\n\n"
        for url, reason in failed:
            entry = f"{html.escape(url)}\n→ {reason}\n\n"
            if len(chunk) + len(entry) > 3500:
                bot.send_message(job["chat_id"], chunk, disable_web_page_preview=True)
                chunk = ""
            chunk += entry
        if chunk:
            bot.send_message(job["chat_id"], chunk, disable_web_page_preview=True)


def worker_loop():
    while True:
        job = jobs.get()
        try:
            run_job(job)
        except Exception:  # noqa: BLE001
            log.exception("Job failed")
        finally:
            jobs.task_done()


# --------------------------------------------------------------------------
# Bot handlers
# --------------------------------------------------------------------------
def is_owner(message) -> bool:
    return message.from_user is not None and message.from_user.id == OWNER_ID


@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    if not is_owner(message):
        bot.reply_to(message, "⛔ Ei bot shudhu owner-er jonno.")
        return
    bot.reply_to(
        message,
        "👋 <b>Welcome!</b>\n\n"
        "Je kono website-er video link pathan. Ami video download kore "
        "channel e upload kore dibo.\n\n"
        "• Ekbare onek link ek message e dite paren.\n"
        "• Direct video/audio file link (.mp4, .mp3) o cholbe.\n"
        "• Video 50 MB-er moddhe hote hobe.",
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("stop:"))
def on_stop(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, "⛔ Owner only", show_alert=True)
        return
    flag = stop_flags.get(call.data.split(":", 1)[1])
    if flag:
        flag.set()
        bot.answer_callback_query(call.id, "⏹ Cholmaan link shesh hole bondho hobe")
    else:
        bot.answer_callback_query(call.id, "Ei kaj ta agei shesh hoye geche")


@bot.message_handler(func=lambda m: m.text is not None)
def handle_text(message):
    if not is_owner(message):
        bot.reply_to(message, "⛔ Ei bot shudhu owner-er jonno.")
        return

    urls = extract_urls(message.text)
    if not urls:
        bot.reply_to(message, "❌ Video-r link pathan (http:// ba https:// diye).")
        return

    status = bot.reply_to(
        message,
        f"⏳ {len(urls)} ta link paoa gelo. Ekta ekta kore download hobe...",
        reply_markup=None,
    )
    job_id = uuid.uuid4().hex[:8]
    stop_flags[job_id] = threading.Event()
    jobs.put(
        {
            "id": job_id,
            "chat_id": message.chat.id,
            "status_id": status.message_id,
            "urls": urls,
        }
    )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
if __name__ == "__main__":
    keep_alive()
    threading.Thread(target=worker_loop, daemon=True).start()
    log.info("Bot is starting... Channel: %s | Owner: %s", CHANNEL_ID, OWNER_ID)
    # skip_pending intentionally NOT used: infinity_polling retries on its own
    # if an old instance still holds the getUpdates connection (409).
    bot.infinity_polling(timeout=30, long_polling_timeout=30)
