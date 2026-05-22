import asyncio
import io
import json
import logging
import os
import random
import tempfile
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from telegram import BotCommand, BotCommandScopeChat, InputFile, Message, Update
from telegram.error import (
    BadRequest,
    Forbidden,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeFilename


BASE_DIR = Path(__file__).resolve().parent
COUNTER_FILE = BASE_DIR / "submission_counter.json"
USERS_FILE = BASE_DIR / "users.json"
DELIVERIES_FILE = BASE_DIR / "deliveries.json"
DEFAULT_SUBMISSION_ID = 1000
DEFAULT_USER_ID_START = 10000
MONTHLY_TOKEN_LIMIT = 500
MAX_SUBSCRIPTION_DAYS = 30
APK_MIME_TYPE = "application/vnd.android.package-archive"
RATE_LIMIT_SECONDS = 3
MAX_APK_SIZE_BYTES = 20 * 1024 * 1024   # 20 MB
SUBMISSION_ID_STEP_MIN = 1               # random step range (keeps IDs always
SUBMISSION_ID_STEP_MAX = 9               #   increasing but unpredictable)
TIMER_UPDATE_INTERVAL = 30       # seconds between live-timer edits
FINAL_CAPTION = " "

counter_lock = asyncio.Lock()
rate_limit_lock = asyncio.Lock()
deliveries_lock = asyncio.Lock()
users_lock = asyncio.Lock()
user_rate_limits: dict[int, float] = {}

# Submission IDs whose timer is still running (in-memory; resets on restart)
active_submission_ids: set[int] = set()

# ── Bot2 integration state ─────────────────────────────────────────────────
telethon_client: Optional[TelegramClient] = None
bot2_conv_lock = asyncio.Lock()   # only one conversation with bot2 at a time
bot2_queue_lock = asyncio.Lock()  # guards bot2_queue_count
bot2_queue_count = 0              # how many APKs are currently waiting or processing


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# .env loader
# ──────────────────────────────────────────────────────────────────────────────

def load_dotenv_file() -> None:
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    try:
        with env_file.open("r", encoding="utf-8") as file:
            for raw_line in file:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError as exc:
        logger.warning("Failed to load .env file: %s", exc)


# ──────────────────────────────────────────────────────────────────────────────
# Submission counter
# ──────────────────────────────────────────────────────────────────────────────

def load_counter() -> int:
    if not COUNTER_FILE.exists():
        return DEFAULT_SUBMISSION_ID
    try:
        with COUNTER_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)
        value = int(data.get("next_submission_id", DEFAULT_SUBMISSION_ID))
        return max(value, DEFAULT_SUBMISSION_ID)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load counter file: %s", exc)
        return DEFAULT_SUBMISSION_ID


def save_counter(next_submission_id: int) -> None:
    COUNTER_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_file = COUNTER_FILE.with_suffix(".tmp")
    payload = {"next_submission_id": int(next_submission_id)}
    with temp_file.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=True, indent=2)
    temp_file.replace(COUNTER_FILE)


# ──────────────────────────────────────────────────────────────────────────────
# Users persistence
# ──────────────────────────────────────────────────────────────────────────────

def migrate_users(data: dict) -> bool:
    """Migrate old integer-format users to full dict records. Returns True if any migrated."""
    changed = False
    for key, value in list(data["users"].items()):
        if isinstance(value, int):
            data["users"][key] = default_user_record(value)
            changed = True
    return changed


def load_users() -> dict:
    if not USERS_FILE.exists():
        return {"users": {}}
    try:
        with USERS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if migrate_users(data):
            save_users(data)
        return data
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load users file: %s", exc)
        return {"users": {}}


def save_users(data: dict) -> None:
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_file = USERS_FILE.with_suffix(".tmp")
    with temp_file.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=True, indent=2)
    temp_file.replace(USERS_FILE)


# ──────────────────────────────────────────────────────────────────────────────
# Deliveries persistence
# deliveries.json format:
#   "admin_chat_id:forwarded_msg_id" -> {
#       "user_chat_id": <int>,
#       "timer_msg_id": <int|null>,
#       "submission_id": <int>
#   }
# Old entries (plain int value) are handled via backward-compat in pop_delivery.
# ──────────────────────────────────────────────────────────────────────────────

def load_deliveries() -> dict:
    if not DELIVERIES_FILE.exists():
        return {}
    try:
        with DELIVERIES_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_deliveries(data: dict) -> None:
    DELIVERIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_file = DELIVERIES_FILE.with_suffix(".tmp")
    with temp_file.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=True, indent=2)
    temp_file.replace(DELIVERIES_FILE)


async def register_delivery(
    admin_chat_id: int,
    admin_msg_id: int,
    user_chat_id: int,
    timer_msg_id: Optional[int],
    submission_id: int,
    user_msg_id: Optional[int] = None,
) -> None:
    async with deliveries_lock:
        data = load_deliveries()
        data[f"{admin_chat_id}:{admin_msg_id}"] = {
            "user_chat_id": user_chat_id,
            "timer_msg_id": timer_msg_id,
            "submission_id": submission_id,
            "user_msg_id": user_msg_id,   # original APK message in user's chat
        }
        save_deliveries(data)
    # Mark submission as active so the timer loop keeps running
    active_submission_ids.add(submission_id)


async def peek_delivery_user(admin_chat_id: int, reply_to_msg_id: int) -> Optional[int]:
    """Return user_chat_id for a delivery WITHOUT removing it (for text message forwarding)."""
    async with deliveries_lock:
        data = load_deliveries()
        entry = data.get(f"{admin_chat_id}:{reply_to_msg_id}")
        if entry is None:
            return None
        if isinstance(entry, dict):
            return entry.get("user_chat_id")
        return entry  # backward compat


async def pop_delivery(
    admin_chat_id: int, reply_to_msg_id: int
) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """Remove delivery entry and return (user_chat_id, timer_msg_id, user_msg_id).
    All None if not found."""
    async with deliveries_lock:
        data = load_deliveries()
        key = f"{admin_chat_id}:{reply_to_msg_id}"
        entry = data.pop(key, None)
        if entry is None:
            return None, None, None
        save_deliveries(data)

    if isinstance(entry, dict):
        user_chat_id = entry.get("user_chat_id")
        timer_msg_id = entry.get("timer_msg_id")
        user_msg_id  = entry.get("user_msg_id")
        sub_id = entry.get("submission_id")
        if sub_id is not None:
            active_submission_ids.discard(sub_id)
        return user_chat_id, timer_msg_id, user_msg_id
    else:
        # Backward compat: old format stored bare user_chat_id int
        return entry, None, None


# ──────────────────────────────────────────────────────────────────────────────
# User helpers
# ──────────────────────────────────────────────────────────────────────────────

def generate_unique_id(existing_ids: set) -> int:
    while True:
        new_id = random.randint(10000, 999999)
        if new_id not in existing_ids:
            return new_id


def default_user_record(user_id: int) -> dict:
    return {
        "user_id": user_id,
        "username": None,
        "first_name": None,
        "paid": False,
        "expiry_date": None,
        "daily_token_limit": 0,
        "tokens_used_today": 0,
        "last_token_reset": str(date.today()),
        "monthly_tokens_used": 0,
        "last_month_reset": date.today().strftime("%Y-%m"),
    }


def find_record_by_bot_id(data: dict, bot_user_id: int) -> Optional[tuple[str, dict]]:
    """Return (telegram_key, record) for the given bot user ID, or None."""
    for tg_key, record in data["users"].items():
        if isinstance(record, dict) and record.get("user_id") == bot_user_id:
            return tg_key, record
    return None


def reset_tokens_if_new_day(record: dict) -> None:
    today = str(date.today())
    this_month = date.today().strftime("%Y-%m")
    if record.get("last_token_reset") != today:
        record["tokens_used_today"] = 0
        record["last_token_reset"] = today
    if record.get("last_month_reset") != this_month:
        record["monthly_tokens_used"] = 0
        record["last_month_reset"] = this_month
    if "monthly_tokens_used" not in record:
        record["monthly_tokens_used"] = 0
    if "last_month_reset" not in record:
        record["last_month_reset"] = this_month


async def get_or_create_user(
    telegram_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
) -> dict:
    async with users_lock:
        data = load_users()
        key = str(telegram_id)
        if key not in data["users"]:
            existing_ids = {
                r["user_id"] for r in data["users"].values() if isinstance(r, dict)
            }
            new_id = generate_unique_id(existing_ids)
            data["users"][key] = default_user_record(new_id)
        record = data["users"][key]
        if not isinstance(record, dict):
            record = default_user_record(record)
            data["users"][key] = record
        # Always refresh username/first_name so they stay up to date
        changed = False
        if username is not None and record.get("username") != username:
            record["username"] = username
            changed = True
        if first_name is not None and record.get("first_name") != first_name:
            record["first_name"] = first_name
            changed = True
        reset_tokens_if_new_day(record)
        save_users(data)
        return record


def is_subscription_active(record: dict) -> bool:
    expiry = record.get("expiry_date")
    if not expiry:
        return False
    try:
        return date.fromisoformat(expiry) >= date.today()
    except ValueError:
        return False


def daily_tokens_remaining(record: dict) -> int:
    limit = record.get("daily_token_limit", 0)
    used = record.get("tokens_used_today", 0)
    return max(0, limit - used)


def monthly_tokens_remaining(record: dict) -> int:
    used = record.get("monthly_tokens_used", 0)
    return max(0, MONTHLY_TOKEN_LIMIT - used)


def tokens_remaining(record: dict) -> int:
    return min(daily_tokens_remaining(record), monthly_tokens_remaining(record))


BLOCK_REASON_NO_ACCESS     = "no_access"
BLOCK_REASON_NOT_PAID      = "not_paid"
BLOCK_REASON_EXPIRED       = "expired"
BLOCK_REASON_DAILY_LIMIT   = "daily_limit"
BLOCK_REASON_MONTHLY_LIMIT = "monthly_limit"


async def consume_token(telegram_id: int) -> tuple[bool, Optional[str]]:
    """Deduct one token. Returns (allowed, reason). Admin always allowed."""
    if is_admin(telegram_id):
        return True, None
    async with users_lock:
        data = load_users()
        key = str(telegram_id)
        if key not in data["users"]:
            return False, BLOCK_REASON_NO_ACCESS
        record = data["users"][key]
        if not isinstance(record, dict):
            return False, BLOCK_REASON_NO_ACCESS

        reset_tokens_if_new_day(record)

        if not record.get("paid", False):
            save_users(data)
            return False, BLOCK_REASON_NOT_PAID

        if not is_subscription_active(record):
            save_users(data)
            return False, BLOCK_REASON_EXPIRED

        if daily_tokens_remaining(record) <= 0:
            save_users(data)
            return False, BLOCK_REASON_DAILY_LIMIT

        if monthly_tokens_remaining(record) <= 0:
            save_users(data)
            return False, BLOCK_REASON_MONTHLY_LIMIT

        record["tokens_used_today"] = record.get("tokens_used_today", 0) + 1
        record["monthly_tokens_used"] = record.get("monthly_tokens_used", 0) + 1
        save_users(data)
        return True, None


# ──────────────────────────────────────────────────────────────────────────────
# APK validation
# ──────────────────────────────────────────────────────────────────────────────

VALID_APK_MIME_TYPES = {
    APK_MIME_TYPE,
    "application/octet-stream",
    "application/zip",
    "application/x-zip-compressed",
}


def is_valid_apk(document) -> bool:
    filename = (document.file_name or "").lower()
    if not filename.endswith(".apk"):
        return False
    mime = document.mime_type or ""
    if mime and mime not in VALID_APK_MIME_TYPES:
        logger.info("APK rejected — unexpected MIME type: %s (file: %s)", mime, filename)
        return False
    return True


# ──────────────────────────────────────────────────────────────────────────────
# Misc helpers
# ──────────────────────────────────────────────────────────────────────────────

async def get_next_submission_id() -> int:
    async with counter_lock:
        submission_id = load_counter()
        step = random.randint(SUBMISSION_ID_STEP_MIN, SUBMISSION_ID_STEP_MAX)
        save_counter(submission_id + step)   # next ID will be current + random step
        return submission_id


async def check_rate_limit(user_id: int) -> float:
    now = time.monotonic()
    async with rate_limit_lock:
        last_seen = user_rate_limits.get(user_id)
        if last_seen is not None:
            elapsed = now - last_seen
            if elapsed < RATE_LIMIT_SECONDS:
                return RATE_LIMIT_SECONDS - elapsed
        user_rate_limits[user_id] = now
        return 0.0


def build_waiting_text(filename: str, submission_id: int, elapsed_seconds: float) -> str:
    """Build the live-timer message shown to users while their APK is being processed."""
    minutes = elapsed_seconds / 60
    return (
        f"{filename}\n\n"
        f"Submission ID: {submission_id}\n\n"
        "Your APK has been received and is being processed by our team.\n"
        "You will receive the protected version shortly. Please wait.\n\n"
        f"⏱ Last updated: {minutes:.2f} minutes since submission"
    )


def build_user_info_text(record: dict) -> str:
    reset_tokens_if_new_day(record)
    paid = record.get("paid", False)
    expiry = record.get("expiry_date") or "N/A"
    daily_limit = record.get("daily_token_limit", 0)
    daily_used = record.get("tokens_used_today", 0)
    monthly_used = record.get("monthly_tokens_used", 0)
    active = is_subscription_active(record) if paid else False
    status = ("Active" if active else "Expired") if paid else "N/A"
    return (
        f"User ID: {record['user_id']}\n"
        f"Paid User: {'Yes' if paid else 'No'}\n"
        f"Subscription: {status}\n"
        f"Expiry Date: {expiry}\n"
        f"Daily Tokens: {daily_used} used / {daily_limit} limit\n"
        f"Monthly Tokens: {monthly_used} used / {MONTHLY_TOKEN_LIMIT} limit\n"
        f"Tokens Available Today: {tokens_remaining(record)}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Telegram safe helpers
# ──────────────────────────────────────────────────────────────────────────────

async def safe_send_message(bot, chat_id: int, text: str) -> Optional[Message]:
    for attempt in range(2):
        try:
            return await bot.send_message(chat_id=chat_id, text=text)
        except RetryAfter as exc:
            if attempt == 0:
                await asyncio.sleep(min(float(exc.retry_after), 5.0))
                continue
            logger.warning("Rate limited while sending message to chat %s", chat_id)
        except (TimedOut, NetworkError) as exc:
            if attempt == 0:
                logger.warning("Temporary send_message failure for chat %s: %s", chat_id, exc)
                await asyncio.sleep(1)
                continue
            logger.warning("Repeated send_message failure for chat %s: %s", chat_id, exc)
        except (BadRequest, Forbidden, TelegramError) as exc:
            logger.warning("Failed to send message to chat %s: %s", chat_id, exc)
            break
    return None


async def safe_send_document(
    bot,
    chat_id: int,
    document_id: str,
    caption: str,
    reply_to_message_id: Optional[int] = None,
) -> Optional[Message]:
    current_reply_id = reply_to_message_id
    for attempt in range(3):
        try:
            return await bot.send_document(
                chat_id=chat_id,
                document=document_id,
                caption=caption,
                reply_to_message_id=current_reply_id,
            )
        except RetryAfter as exc:
            if attempt < 2:
                await asyncio.sleep(min(float(exc.retry_after), 5.0))
                continue
            logger.warning("Rate limited while sending document to chat %s", chat_id)
        except (TimedOut, NetworkError) as exc:
            if attempt < 2:
                logger.warning("Temporary send_document failure for chat %s: %s", chat_id, exc)
                await asyncio.sleep(1)
                continue
            logger.warning("Repeated send_document failure for chat %s: %s", chat_id, exc)
        except BadRequest as exc:
            err = str(exc).lower()
            if current_reply_id and ("reply" in err or "message" in err) and attempt < 2:
                # Original message deleted — retry without reply_to
                logger.info("Reply message not found for chat %s, retrying without reply", chat_id)
                current_reply_id = None
                continue
            logger.warning("Failed to send document to chat %s: %s", chat_id, exc)
            break
        except (Forbidden, TelegramError) as exc:
            logger.warning("Failed to send document to chat %s: %s", chat_id, exc)
            break
    return None


async def safe_delete_message(message: Optional[Message]) -> bool:
    if message is None:
        return False
    try:
        await message.delete()
        return True
    except (BadRequest, Forbidden, TelegramError) as exc:
        logger.info("Skipping message delete for chat %s: %s", message.chat_id, exc)
        return False


async def safe_delete_by_id(bot, chat_id: int, message_id: int) -> bool:
    """Delete a message identified by chat_id + message_id."""
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except (BadRequest, Forbidden, TelegramError) as exc:
        logger.info("Skipping delete of msg %s in chat %s: %s", message_id, chat_id, exc)
        return False


async def safe_edit_message(message: Optional[Message], text: str) -> Optional[bool]:
    if message is None:
        return None
    try:
        await message.edit_text(text=text)
        return True
    except BadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return False
        logger.info("Skipping message edit for chat %s: %s", message.chat_id, exc)
        return None
    except (Forbidden, TelegramError) as exc:
        logger.info("Skipping message edit for chat %s: %s", message.chat_id, exc)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Live-timer background task
# ──────────────────────────────────────────────────────────────────────────────

async def run_submission_flow(
    bot,
    filename: str,
    submission_id: int,
    started_at: float,
    timer_message: Optional[Message],
) -> None:
    """
    Loop that updates the user's waiting message with elapsed time every
    TIMER_UPDATE_INTERVAL seconds.  Stops when the submission ID is removed
    from active_submission_ids (i.e. admin has replied with the processed APK).
    """
    try:
        while submission_id in active_submission_ids:
            elapsed = time.monotonic() - started_at
            text = build_waiting_text(filename, submission_id, elapsed)
            await safe_edit_message(timer_message, text)
            # Sleep in small chunks so we react quickly when the submission is done
            for _ in range(TIMER_UPDATE_INTERVAL):
                if submission_id not in active_submission_ids:
                    return
                await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Unhandled error in timer loop for submission %s", submission_id)


# ──────────────────────────────────────────────────────────────────────────────
# Bot2 integration
# ──────────────────────────────────────────────────────────────────────────────

def _save_session_to_env(session_str: str) -> None:
    env_file = BASE_DIR / ".env"
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines(keepends=True) if env_file.exists() else []
        new_line = f"TELETHON_SESSION={session_str}\n"
        for i, line in enumerate(lines):
            if line.startswith("TELETHON_SESSION="):
                lines[i] = new_line
                break
        else:
            lines.append(new_line)
        env_file.write_text("".join(lines), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not save session to .env: %s", exc)


async def _process_via_bot2(
    bot,
    document,
    filename: str,
    user_chat_id: int,
    user_msg_id: int,
    timer_msg_id: Optional[int],
    submission_id: int,
) -> None:
    """Send APK to bot2 via Telethon conversation, wait for document reply, deliver to user."""
    global bot2_queue_count

    try:
        tg_file = await bot.get_file(document.file_id)
        file_bytes = bytes(await tg_file.download_as_bytearray())
    except Exception as exc:
        logger.warning("Failed to download APK for bot2 flow: %s", exc)
        return

    bot2_username = os.getenv("BOT2_USERNAME", "").lstrip("@")
    send_buf = io.BytesIO(file_bytes)
    send_buf.name = filename

    # ── Track queue position ──────────────────────────────────────────────────
    async with bot2_queue_lock:
        bot2_queue_count += 1
        my_position = bot2_queue_count

    try:
        # If others are ahead in the queue, tell the user their APK is waiting
        if my_position > 1 and timer_msg_id is not None:
            try:
                await bot.edit_message_text(
                    chat_id=user_chat_id,
                    message_id=timer_msg_id,
                    text=(
                        f"📂 {filename}\n\n"
                        f"Submission ID: {submission_id}\n\n"
                        f"⏳ Queued — position #{my_position - 1} in line.\n"
                        "Your APK will be processed shortly. Please wait."
                    ),
                )
            except Exception:
                pass
        logger.info("APK %s queued for bot2 (position %d)", filename, my_position)

    except Exception:
        pass

    try:
        tmp_path: Optional[str] = None
        try:
            async with bot2_conv_lock:
                # Update message to show processing has started
                if timer_msg_id is not None:
                    try:
                        await bot.edit_message_text(
                            chat_id=user_chat_id,
                            message_id=timer_msg_id,
                            text=(
                                f"📂 {filename}\n\n"
                                f"Submission ID: {submission_id}\n\n"
                                "🔄 Processing your APK now...\n"
                                "Please wait, this usually takes a few minutes."
                            ),
                        )
                    except Exception:
                        pass
                async with telethon_client.conversation(bot2_username, timeout=700) as conv:
                    await conv.send_file(
                        send_buf,
                        force_document=True,
                        attributes=[DocumentAttributeFilename(file_name=filename)],
                        workers=4,
                    )
                    logger.info("APK sent to bot2 (%s), waiting for response...", filename)

                    # Skip any text/status messages — wait until we get a document
                    while True:
                        response = await conv.get_response()
                        if response.document:
                            break
                        logger.info("bot2 sent non-document message, waiting for APK...")

                    # Download to a temp file — avoids LOCATION_NOT_AVAILABLE on large files
                    tmp_path = await telethon_client.download_media(
                        response, file=tempfile.gettempdir()
                    )
                    logger.info("bot2 file downloaded to temp: %s", tmp_path)

            if not tmp_path:
                await safe_send_message(bot, user_chat_id, "Bot2 returned an empty file.")
                return

            # Delete the waiting/timer message before delivering the result
            active_submission_ids.discard(submission_id)
            if timer_msg_id is not None:
                await safe_delete_by_id(bot, user_chat_id, timer_msg_id)

            delivered = False
            for reply_id in ([user_msg_id, None] if user_msg_id else [None]):
                try:
                    with open(tmp_path, "rb") as f:
                        await bot.send_document(
                            chat_id=user_chat_id,
                            document=InputFile(f, filename=filename),
                            reply_to_message_id=reply_id,
                            read_timeout=300,
                            write_timeout=300,
                            connect_timeout=60,
                        )
                    delivered = True
                    break
                except BadRequest as exc:
                    err = str(exc).lower()
                    if reply_id and ("reply" in err or "message" in err):
                        logger.info("Reply msg gone for user %s, retrying without reply", user_chat_id)
                        continue
                    raise
            if delivered:
                logger.info("bot2 APK delivered to user %s", user_chat_id)

        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    except asyncio.TimeoutError:
        await safe_send_message(bot, user_chat_id, "Processing timed out (server took too long). Please try again.")
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("bot2 error for user %s", user_chat_id)
        await safe_send_message(bot, user_chat_id, "Failed to deliver processed file.")
    finally:
        async with bot2_queue_lock:
            bot2_queue_count = max(0, bot2_queue_count - 1)


# ──────────────────────────────────────────────────────────────────────────────
# Admin helpers
# ──────────────────────────────────────────────────────────────────────────────

def is_admin(telegram_id: int) -> bool:
    admin_ids = os.getenv("ADMIN_TELEGRAM_ID", "")
    return str(telegram_id) in {a.strip() for a in admin_ids.split(",") if a.strip()}


async def admin_only(update: Update) -> bool:
    if update.effective_user is None or not is_admin(update.effective_user.id):
        return False
    return True


# ──────────────────────────────────────────────────────────────────────────────
# Command handlers
# ──────────────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_user is None:
        return

    tg_user = update.effective_user
    record = await get_or_create_user(
        tg_user.id,
        username=tg_user.username,
        first_name=tg_user.first_name,
    )
    paid = record.get("paid", False)

    welcome = (
        build_user_info_text(record) + "\n\n"
        + (
            "Now you can send an APK file to this bot."
            if paid else
            "Trial version: Each APK you get can only work 14 days for you to test. "
            "Contact the developer to pay for the full version if it works for you.\n\n"
            "Now you can send an APK file to this bot to test."
        )
    )

    await safe_send_message(context.bot, update.effective_chat.id, welcome)


async def info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_user is None:
        return

    tg_user = update.effective_user
    record = await get_or_create_user(
        tg_user.id,
        username=tg_user.username,
        first_name=tg_user.first_name,
    )
    await safe_send_message(context.bot, update.effective_chat.id, build_user_info_text(record))


async def cmd_setpaid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_only(update):
        return
    args = context.args
    if not args or len(args) < 2:
        await safe_send_message(context.bot, update.effective_chat.id,
                                "Usage: /setpaid <user_id> yes|no")
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await safe_send_message(context.bot, update.effective_chat.id, "Invalid user ID.")
        return
    paid = args[1].lower() in ("yes", "true", "1")
    async with users_lock:
        data = load_users()
        result = find_record_by_bot_id(data, target_id)
        if not result:
            await safe_send_message(context.bot, update.effective_chat.id,
                                    f"User {target_id} not found.")
            return
        tg_key, record = result
        record["paid"] = paid
        if not paid:
            record["daily_token_limit"] = 0
            record["expiry_date"] = None
        save_users(data)
    status = "Paid" if paid else "Unpaid (tokens and expiry reset)"
    await safe_send_message(context.bot, update.effective_chat.id,
                            f"User {target_id} marked as {status}.")


async def cmd_setexpiry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_only(update):
        return
    args = context.args
    if not args or len(args) < 2:
        await safe_send_message(context.bot, update.effective_chat.id,
                                "Usage: /setexpiry <user_id> YYYY-MM-DD")
        return
    try:
        target_id = int(args[0])
        expiry_date = date.fromisoformat(args[1])
    except ValueError:
        await safe_send_message(context.bot, update.effective_chat.id,
                                "Invalid ID or date. Use YYYY-MM-DD format.")
        return
    max_date = date.today() + __import__("datetime").timedelta(days=MAX_SUBSCRIPTION_DAYS)
    if expiry_date > max_date:
        await safe_send_message(context.bot, update.effective_chat.id,
                                f"Max subscription is {MAX_SUBSCRIPTION_DAYS} days. Latest allowed: {max_date}.")
        return
    async with users_lock:
        data = load_users()
        result = find_record_by_bot_id(data, target_id)
        if not result:
            await safe_send_message(context.bot, update.effective_chat.id,
                                    f"User {target_id} not found.")
            return
        tg_key, record = result
        if not record.get("paid", False):
            await safe_send_message(context.bot, update.effective_chat.id,
                                    f"User {target_id} is not a paid user. Use /setpaid {target_id} yes first.")
            return
        record["expiry_date"] = args[1]
        save_users(data)
    await safe_send_message(context.bot, update.effective_chat.id,
                            f"User {target_id} expiry set to {args[1]}.")


async def cmd_settokens(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_only(update):
        return
    args = context.args
    if not args or len(args) < 2:
        await safe_send_message(context.bot, update.effective_chat.id,
                                "Usage: /settokens <user_id> <daily_limit>")
        return
    try:
        target_id = int(args[0])
        limit = int(args[1])
        if limit < 0:
            raise ValueError
    except ValueError:
        await safe_send_message(context.bot, update.effective_chat.id,
                                "Invalid user ID or token limit.")
        return
    async with users_lock:
        data = load_users()
        result = find_record_by_bot_id(data, target_id)
        if not result:
            await safe_send_message(context.bot, update.effective_chat.id,
                                    f"User {target_id} not found.")
            return
        tg_key, record = result
        if not record.get("paid", False):
            await safe_send_message(context.bot, update.effective_chat.id,
                                    f"User {target_id} is not a paid user. Use /setpaid {target_id} yes first.")
            return
        record["daily_token_limit"] = limit
        save_users(data)
    await safe_send_message(context.bot, update.effective_chat.id,
                            f"User {target_id} daily token limit set to {limit}/day (monthly cap: {MONTHLY_TOKEN_LIMIT}).")


async def cmd_userinfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_only(update):
        return
    args = context.args
    if not args:
        await safe_send_message(context.bot, update.effective_chat.id,
                                "Usage: /userinfo <user_id>")
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await safe_send_message(context.bot, update.effective_chat.id, "Invalid user ID.")
        return
    async with users_lock:
        data = load_users()
        result = find_record_by_bot_id(data, target_id)
        if not result:
            await safe_send_message(context.bot, update.effective_chat.id,
                                    f"User {target_id} not found.")
            return
        tg_key, record = result
    reset_tokens_if_new_day(record)
    paid = record.get("paid", False)
    active = is_subscription_active(record) if paid else False
    uname = f"@{record['username']}" if record.get("username") else "No username"
    fname = record.get("first_name") or "N/A"
    text = (
        f"User ID: {record['user_id']}\n"
        f"Name: {fname}\n"
        f"Username: {uname}\n"
        f"Telegram ID: {tg_key}\n"
        f"Paid: {'Yes' if paid else 'No'}\n"
        f"Subscription: {'Active' if active else 'Expired/None'}\n"
        f"Expiry Date: {record.get('expiry_date') or 'N/A'}\n"
        f"Daily Token Limit: {record.get('daily_token_limit', 0)}\n"
        f"Daily Tokens Used: {record.get('tokens_used_today', 0)}\n"
        f"Daily Tokens Left: {daily_tokens_remaining(record)}\n"
        f"Monthly Tokens Used: {record.get('monthly_tokens_used', 0)} / {MONTHLY_TOKEN_LIMIT}\n"
        f"Monthly Tokens Left: {monthly_tokens_remaining(record)}"
    )
    await safe_send_message(context.bot, update.effective_chat.id, text)


async def cmd_listusers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_only(update):
        return
    async with users_lock:
        data = load_users()
    users = data.get("users", {})
    if not users:
        await safe_send_message(context.bot, update.effective_chat.id, "No users yet.")
        return
    lines = []
    for tg_key, record in users.items():
        if not isinstance(record, dict):
            continue
        reset_tokens_if_new_day(record)
        paid = "Paid" if record.get("paid") else "Trial"
        expiry = record.get("expiry_date") or "N/A"
        remaining = tokens_remaining(record)
        limit = record.get("daily_token_limit", 0)
        uname = f"@{record['username']}" if record.get("username") else "No username"
        fname = record.get("first_name") or "N/A"
        lines.append(
            f"ID: {record['user_id']} | {fname} | {uname} | {paid} | Expiry: {expiry} | Tokens: {remaining}/{limit}"
        )
    if not lines:
        await safe_send_message(context.bot, update.effective_chat.id, "No users yet.")
        return
    # Send header first
    await safe_send_message(context.bot, update.effective_chat.id,
                            f"Total users: {len(lines)}")
    # Split into chunks of max 4000 chars to stay under Telegram's 4096 limit
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 4000:
            await safe_send_message(context.bot, update.effective_chat.id, chunk)
            chunk = line
        else:
            chunk = chunk + "\n" + line if chunk else line
    if chunk:
        await safe_send_message(context.bot, update.effective_chat.id, chunk)


# ──────────────────────────────────────────────────────────────────────────────
# Admin text message handler
# ──────────────────────────────────────────────────────────────────────────────

async def handle_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Allow admin to send a text message to a user by replying to the forwarded APK."""
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if message is None or chat is None or user is None:
        return

    # Only admins, only replies
    if not is_admin(user.id) or message.reply_to_message is None:
        return

    reply_to_id = message.reply_to_message.message_id
    user_chat_id = await peek_delivery_user(chat.id, reply_to_id)

    if user_chat_id is None:
        # Not a tracked delivery — silently ignore
        return

    text = message.text or ""
    if not text.strip():
        return

    sent = await safe_send_message(context.bot, user_chat_id, text)
    if sent:
        await safe_send_message(context.bot, chat.id,
                                f"✅ Message sent to user (chat ID: {user_chat_id}).")
    else:
        await safe_send_message(context.bot, chat.id,
                                f"❌ Failed to send message to user (chat ID: {user_chat_id}). "
                                "They may have blocked the bot.")


# ──────────────────────────────────────────────────────────────────────────────
# Main document handler
# ──────────────────────────────────────────────────────────────────────────────

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Single handler for all documents. Routes to delivery or APK submission."""
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if message is None or chat is None or user is None or message.document is None:
        return

    # ── ADMIN REPLY PATH: deliver processed APK to user ──────────────────────
    if is_admin(user.id) and message.reply_to_message is not None:
        reply_to_id = message.reply_to_message.message_id
        user_chat_id, timer_msg_id, user_msg_id = await pop_delivery(chat.id, reply_to_id)

        if user_chat_id is None:
            await safe_send_message(context.bot, chat.id,
                                    "No pending delivery found for this message.")
            return

        # Delete the live-timer waiting message from the user's chat
        if timer_msg_id is not None:
            await safe_delete_by_id(context.bot, user_chat_id, timer_msg_id)

        # Send the protected APK as a reply to the user's original APK message
        # so the user can clearly see which APK was processed
        sent = await safe_send_document(
            context.bot, user_chat_id, message.document.file_id, FINAL_CAPTION,
            reply_to_message_id=user_msg_id,
        )
        if sent:
            await safe_send_message(context.bot, chat.id,
                                    f"✅ APK delivered to user (chat ID: {user_chat_id}).")
        else:
            await safe_send_message(context.bot, chat.id,
                                    f"❌ Failed to deliver to user (chat ID: {user_chat_id}). "
                                    "User may have blocked the bot or deleted their account.")
        return  # ← STOP. Never falls through to APK submission logic.

    # ── USER APK SUBMISSION PATH ──────────────────────────────────────────────
    document = message.document
    if not is_valid_apk(document):
        return

    # Size guard — reject APKs larger than 20 MB
    if document.file_size and document.file_size > MAX_APK_SIZE_BYTES:
        await safe_send_message(context.bot, chat.id, "Max apk size is 20MB")
        return

    retry_after = await check_rate_limit(user.id)
    if retry_after > 0:
        await safe_send_message(
            context.bot,
            chat.id,
            f"Please wait {retry_after:.1f} seconds before sending another APK.",
        )
        return

    allowed, reason = await consume_token(user.id)
    if not allowed:
        record = await get_or_create_user(
            user.id, username=user.username, first_name=user.first_name
        )
        if reason == BLOCK_REASON_NOT_PAID:
            msg = "Access denied. Contact the developer to purchase a subscription."
        elif reason == BLOCK_REASON_EXPIRED:
            msg = f"Your subscription expired on {record.get('expiry_date')}. Contact the developer to renew."
        elif reason == BLOCK_REASON_DAILY_LIMIT:
            msg = f"Daily limit reached ({record.get('daily_token_limit', 0)} APKs/day). Come back tomorrow."
        elif reason == BLOCK_REASON_MONTHLY_LIMIT:
            used = record.get("monthly_tokens_used", 0)
            msg = f"Monthly limit reached ({used}/{MONTHLY_TOKEN_LIMIT} APKs this month). Resets next month."
        else:
            msg = "You have no access. Contact the developer."
        await safe_send_message(context.bot, chat.id, msg)
        return

    submission_id = await get_next_submission_id()
    filename = document.file_name or "package.apk"
    started_at = time.monotonic()
    user_msg_id = message.message_id   # original APK message — used for reply-quote on delivery

    # 1. Forward APK to all admins; collect (admin_id, forwarded_msg_id) pairs
    admin_ids_raw = os.getenv("ADMIN_TELEGRAM_ID", "")
    forwarded_pairs: list[tuple[int, int]] = []
    for admin_id_str in admin_ids_raw.split(","):
        admin_id_str = admin_id_str.strip()
        if not admin_id_str.isdigit():
            continue
        try:
            forwarded = await message.forward(chat_id=int(admin_id_str))
            if forwarded is not None:
                forwarded_pairs.append((int(admin_id_str), forwarded.message_id))
        except Exception as exc:
            logger.warning("Failed to forward APK to admin %s: %s", admin_id_str, exc)

    # 2. Send the initial waiting / timer message to the user
    timer_message = await safe_send_message(
        context.bot,
        chat.id,
        build_waiting_text(filename, submission_id, 0.0),
    )
    timer_msg_id = timer_message.message_id if timer_message else None

    # 3. Register all deliveries (timer_msg_id + user_msg_id stored for delivery step)
    for admin_id, fwd_msg_id in forwarded_pairs:
        await register_delivery(admin_id, fwd_msg_id, chat.id, timer_msg_id, submission_id,
                                user_msg_id=user_msg_id)

    # 4. Start the live-timer background task
    if forwarded_pairs:
        context.application.create_task(
            run_submission_flow(
                bot=context.bot,
                filename=filename,
                submission_id=submission_id,
                started_at=started_at,
                timer_message=timer_message,
            )
        )

    # ── Bot2 automated processing flow (started after timer message exists) ───
    if telethon_client is not None and os.getenv("BOT2_USERNAME", ""):
        context.application.create_task(
            _process_via_bot2(
                bot=context.bot,
                document=document,
                filename=filename,
                user_chat_id=chat.id,
                user_msg_id=user_msg_id,
                timer_msg_id=timer_msg_id,
                submission_id=submission_id,
            )
        )
    # If no admins were reachable we still leave the static message up for the user.


# ──────────────────────────────────────────────────────────────────────────────
# Admin broadcast command
# ──────────────────────────────────────────────────────────────────────────────

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Admin-only: broadcast a message to every registered user (paid + unpaid).

    Two modes:
      /broadcast <text>   — sends the typed text to all users
      Reply to any msg with /broadcast — forwards that message to all users
    """
    if not await admin_only(update):
        return

    message = update.effective_message
    chat    = update.effective_chat
    if message is None or chat is None:
        return

    # ── Determine what to broadcast ──────────────────────────────────────────
    reply_msg   = message.reply_to_message
    inline_text = " ".join(context.args).strip() if context.args else ""

    if not reply_msg and not inline_text:
        await safe_send_message(
            context.bot, chat.id,
            "Usage:\n"
            "• /broadcast <your message>  — broadcast text\n"
            "• Reply to any message with /broadcast — forward that message",
        )
        return

    # Collect all registered user Telegram IDs
    async with users_lock:
        data = load_users()
        user_ids = [int(tid) for tid in data["users"].keys()]

    if not user_ids:
        await safe_send_message(context.bot, chat.id, "No registered users to broadcast to.")
        return

    # Notify admin that broadcast started
    status_msg = await safe_send_message(
        context.bot, chat.id,
        f"📢 Broadcasting to {len(user_ids)} users...",
    )

    sent = failed = blocked = 0

    for tid in user_ids:
        try:
            if reply_msg:
                await reply_msg.forward(chat_id=tid)
            else:
                await context.bot.send_message(chat_id=tid, text=inline_text)
            sent += 1
        except Forbidden:
            blocked += 1
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.5)
            try:
                if reply_msg:
                    await reply_msg.forward(chat_id=tid)
                else:
                    await context.bot.send_message(chat_id=tid, text=inline_text)
                sent += 1
            except Exception:
                failed += 1
        except Exception as exc:
            logger.warning("Broadcast cmd: failed to send to %s: %s", tid, exc)
            failed += 1
        await asyncio.sleep(0.05)   # ~20 msgs/sec

    logger.info("Admin broadcast — sent: %d | blocked: %d | failed: %d", sent, blocked, failed)

    result = (
        f"✅ Broadcast complete!\n"
        f"• Sent:    {sent}\n"
        f"• Blocked: {blocked}  (users who blocked the bot)\n"
        f"• Failed:  {failed}"
    )
    if status_msg:
        await safe_edit_message(status_msg, result)
    else:
        await safe_send_message(context.bot, chat.id, result)


# ──────────────────────────────────────────────────────────────────────────────
# Channel broadcast handler
# ──────────────────────────────────────────────────────────────────────────────

async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Forward any post from the configured channel to ALL registered users (paid + unpaid)."""
    message = update.channel_post
    if message is None:
        return

    channel_id_cfg = os.getenv("CHANNEL_ID", "").strip()
    if not channel_id_cfg:
        return  # feature disabled — CHANNEL_ID not set

    # Match configured channel by @username or numeric ID
    chat = message.chat
    matched = False
    if channel_id_cfg.startswith("@") and chat.username:
        matched = chat.username.lower() == channel_id_cfg.lstrip("@").lower()
    else:
        try:
            matched = int(channel_id_cfg) == chat.id
        except ValueError:
            pass

    if not matched:
        return

    # Collect all registered telegram IDs (paid AND unpaid)
    async with users_lock:
        data = load_users()
        user_ids = [int(tid) for tid in data["users"].keys()]

    if not user_ids:
        logger.info("Channel broadcast: no registered users.")
        return

    logger.info("Channel broadcast: forwarding post to %d users...", len(user_ids))
    sent = failed = blocked = 0

    for tid in user_ids:
        try:
            await message.forward(chat_id=tid)
            sent += 1
        except Forbidden:
            # User has blocked the bot — skip silently
            blocked += 1
        except RetryAfter as exc:
            # Telegram flood limit — wait and retry once
            await asyncio.sleep(float(exc.retry_after) + 0.5)
            try:
                await message.forward(chat_id=tid)
                sent += 1
            except Exception:
                failed += 1
        except Exception as exc:
            logger.warning("Channel broadcast: failed to send to %s: %s", tid, exc)
            failed += 1
        # ~20 msgs/sec — well within Telegram's flood limits
        await asyncio.sleep(0.05)

    logger.info(
        "Channel broadcast complete — sent: %d | blocked: %d | failed: %d",
        sent, blocked, failed,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Application setup
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    import sys
    if sys.platform == "win32":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except AttributeError:
            pass  # removed in Python 3.16+
    # Python 3.12+ no longer auto-creates an event loop — set one explicitly
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    load_dotenv_file()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in the environment or in a .env file.")

    if not COUNTER_FILE.exists():
        save_counter(DEFAULT_SUBMISSION_ID)

    application = Application.builder().token(token).concurrent_updates(True).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("info", info))
    application.add_handler(CommandHandler("setpaid", cmd_setpaid))
    application.add_handler(CommandHandler("setexpiry", cmd_setexpiry))
    application.add_handler(CommandHandler("settokens", cmd_settokens))
    application.add_handler(CommandHandler("userinfo", cmd_userinfo))
    application.add_handler(CommandHandler("listusers", cmd_listusers))
    application.add_handler(CommandHandler("broadcast", cmd_broadcast))
    # Admin text reply → forwards message to the corresponding user
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_text))
    # Single document handler — routes internally to delivery or APK submission
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    # Channel post → broadcast to all registered users (paid + unpaid)
    application.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, handle_channel_post))

    async def post_init(app) -> None:
        await app.bot.set_my_commands([
            BotCommand("start", "Start the bot"),
            BotCommand("info", "Check your user ID and account details"),
        ])
        await app.bot.set_my_description("Testing files\n\n")

        # ── Start Telethon client for bot2 integration ─────────────────────
        api_id  = os.getenv("API_ID", "")
        api_hash = os.getenv("API_HASH", "")
        phone    = os.getenv("PHONE_NUMBER", "")
        bot2_uname = os.getenv("BOT2_USERNAME", "")
        if api_id and api_hash and phone and bot2_uname:
            global telethon_client
            session_str = os.getenv("TELETHON_SESSION", "")
            session = StringSession(session_str) if session_str else StringSession()
            client = TelegramClient(session, int(api_id), api_hash)
            await client.start(phone=phone)
            # Auto-save session string to .env so next restart skips OTP
            new_session_str = client.session.save()
            if new_session_str != session_str:
                _save_session_to_env(new_session_str)
                os.environ["TELETHON_SESSION"] = new_session_str
                logger.info("Telethon session saved to .env")
            telethon_client = client
            logger.info("Telethon client started — bot2: %s", bot2_uname)
        else:
            logger.info("Bot2 env vars not set — bot2 integration disabled")

        admin_commands = [
            BotCommand("start",     "Start the bot"),
            BotCommand("info",      "Check your user ID and account details"),
            BotCommand("listusers", "List all users"),
            BotCommand("userinfo",  "Get details of a user — /userinfo <id>"),
            BotCommand("setpaid",   "Set paid status — /setpaid <id> yes|no"),
            BotCommand("settokens", "Set daily token limit — /settokens <id> <limit>"),
            BotCommand("setexpiry", "Set subscription expiry — /setexpiry <id> YYYY-MM-DD"),
            BotCommand("broadcast", "Broadcast a message to all users — /broadcast <text>"),
        ]
        admin_ids_raw = os.getenv("ADMIN_TELEGRAM_ID", "")
        for admin_id_str in admin_ids_raw.split(","):
            admin_id_str = admin_id_str.strip()
            if admin_id_str.isdigit():
                try:
                    await app.bot.set_my_commands(
                        admin_commands,
                        scope=BotCommandScopeChat(chat_id=int(admin_id_str)),
                    )
                    logger.info("Admin commands set for %s", admin_id_str)
                except Exception as exc:
                    logger.warning("Could not set admin commands for %s: %s", admin_id_str, exc)

    async def post_shutdown(app) -> None:
        if telethon_client is not None:
            await telethon_client.disconnect()
            logger.info("Telethon client disconnected")

    application.post_init = post_init
    application.post_shutdown = post_shutdown
    application.run_polling()


if __name__ == "__main__":
    main()
