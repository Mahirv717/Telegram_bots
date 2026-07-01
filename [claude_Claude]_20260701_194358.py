#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Giveaway Bot
======================
Single-file production-ready Telegram giveaway bot built with aiogram 3.x.

Features:
- Admin-only giveaway creation with configurable duration (e.g. "3h", "10m", "30m")
- Only one active giveaway at a time (starting a new one cancels the previous one)
- Required-channel membership verification before joining
- Duplicate-join prevention
- Admin gets a private notification for every new participant
- /participants exports a participants.txt file (admin only)
- Automatic winner selection + announcement when the timer expires
- Full JSON persistence -> survives restarts (giveaway state + participants)
- Designed to run on Termux (Android) with plain asyncio, no external services

Author: Generated for production use.
"""

import asyncio
import html
import json
import logging
import os
import random
import re
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import Command, BaseFilter
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    FSInputFile,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramAPIError

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # python-dotenv is optional at runtime; the bot still works with the
    # hardcoded defaults below if the package is not installed.
    pass


# ============================================================================
# CONFIGURATION
# ============================================================================

# Bot token — put it in a .env file as BOT_TOKEN=xxxx or hardcode it here.
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")

# Telegram user ID of the ONE admin allowed to control the bot.
ADMIN_ID: int = int(os.getenv("ADMIN_ID", "123456789"))

# The channel where giveaway posts (start/winner announcement) are sent.
# Can be a numeric chat id (e.g. -1001234567890) or a public @username.
GIVEAWAY_CHANNEL_ID: str = os.getenv("GIVEAWAY_CHANNEL_ID", "@your_giveaway_channel")

# Channels users MUST join before they can participate.
# "chat_id" is used for get_chat_member (numeric id or @username).
# "url" is used for the "Join Channel" button.
# The bot MUST be an administrator in every one of these channels.
REQUIRED_CHANNELS = [
    {
        "title": "Main Channel",
        "chat_id": "@c31kincelIer",
        "url": "https://t.me/c31kincelIer",
    },
    # Add more required channels here, e.g.:
    # {
    #     "title": "Second Channel",
    #     "chat_id": "@another_channel",
    #     "url": "https://t.me/another_channel",
    # },
]

# ============================================================================
# STORAGE PATHS
# ============================================================================

DATA_DIR = "data"
GIVEAWAY_FILE = os.path.join(DATA_DIR, "giveaway.json")
PARTICIPANTS_FILE = os.path.join(DATA_DIR, "participants.json")
PARTICIPANTS_TXT = os.path.join(DATA_DIR, "participants.txt")
LOG_FILE = "bot.log"

# ============================================================================
# LOGGING
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("giveaway_bot")


# ============================================================================
# DATA LAYER — JSON persistence helpers
# ============================================================================

class JsonStore:
    """
    Small helper around a single JSON file.
    Automatically creates the file (and its parent directory) with a default
    value if it is missing, empty, or corrupted, so the bot never crashes
    because of a missing/broken data file.
    """

    def __init__(self, path: str, default):
        self.path = path
        self.default = default
        self._lock = asyncio.Lock()
        self._ensure_exists()

    def _ensure_exists(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        if not os.path.isfile(self.path):
            self._write_sync(self.default)

    def _write_sync(self, data):
        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.path)

    def read_sync(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    raise ValueError("empty file")
                return json.loads(content)
        except (json.JSONDecodeError, ValueError, FileNotFoundError, OSError) as e:
            logger.warning("Data file %s missing/corrupted (%s). Recreating default.", self.path, e)
            self._write_sync(self.default)
            return json.loads(json.dumps(self.default))  # deep-copy default

    async def read(self):
        async with self._lock:
            return await asyncio.to_thread(self.read_sync)

    async def write(self, data):
        async with self._lock:
            await asyncio.to_thread(self._write_sync, data)


# Default structures
DEFAULT_GIVEAWAY = {
    "active": False,
    "ended": True,
    "start_time": None,      # ISO string
    "end_time": None,        # ISO string
    "duration_text": None,   # e.g. "3h"
    "winner_id": None,
}

DEFAULT_PARTICIPANTS: dict = {}

giveaway_store = JsonStore(GIVEAWAY_FILE, DEFAULT_GIVEAWAY)
participants_store = JsonStore(PARTICIPANTS_FILE, DEFAULT_PARTICIPANTS)


# ============================================================================
# GIVEAWAY MANAGER
# ============================================================================

class GiveawayManager:
    """
    Encapsulates all giveaway business logic:
    creation, cancellation, participant handling, and automatic ending.
    """

    def __init__(self, bot: Bot):
        self.bot = bot
        self.task: Optional[asyncio.Task] = None

    # ---------------------------------------------------------------- utils

    @staticmethod
    def parse_duration(text: str) -> Optional[int]:
        """Parse strings like '3h', '10m', '30m', '45s', '1d' into seconds."""
        match = re.fullmatch(r"(\d+)\s*([dhms])", text.strip().lower())
        if not match:
            return None
        value, unit = int(match.group(1)), match.group(2)
        if value <= 0:
            return None
        multipliers = {"d": 86400, "h": 3600, "m": 60, "s": 1}
        return value * multipliers[unit]

    @staticmethod
    def now() -> datetime:
        return datetime.now()

    @staticmethod
    def display_name(username: Optional[str], full_name: str) -> str:
        return f"@{username}" if username else full_name

    # ------------------------------------------------------------- state io

    async def get_state(self) -> dict:
        return await giveaway_store.read()

    async def save_state(self, state: dict):
        await giveaway_store.write(state)

    async def get_participants(self) -> dict:
        return await participants_store.read()

    async def save_participants(self, data: dict):
        await participants_store.write(data)

    # -------------------------------------------------------- core actions

    async def start_giveaway(self, duration_text: str, seconds: int) -> dict:
        """Cancel any running giveaway and start a fresh one."""
        await self.cancel_giveaway(silent=True)

        start = self.now()
        end = start + timedelta(seconds=seconds)
        state = {
            "active": True,
            "ended": False,
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "duration_text": duration_text,
            "winner_id": None,
        }
        await self.save_state(state)
        await self.save_participants({})  # fresh participant list

        self._schedule_end(seconds)
        return state

    async def cancel_giveaway(self, silent: bool = False) -> bool:
        """Cancel the current giveaway (if any). Returns True if one was cancelled."""
        state = await self.get_state()
        was_active = state.get("active", False)

        if self.task and not self.task.done():
            self.task.cancel()
        self.task = None

        if was_active:
            state["active"] = False
            state["ended"] = True
            await self.save_state(state)
            if not silent:
                logger.info("Giveaway cancelled by admin.")
        return was_active

    def _schedule_end(self, delay_seconds: float):
        if self.task and not self.task.done():
            self.task.cancel()
        self.task = asyncio.create_task(self._end_after_delay(delay_seconds))

    async def _end_after_delay(self, delay_seconds: float):
        try:
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            await self.end_giveaway()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Error while auto-ending the giveaway.")

    async def restore_on_startup(self):
        """Resume a giveaway that was active before the bot restarted."""
        state = await self.get_state()
        if not state.get("active"):
            return

        try:
            end_time = datetime.fromisoformat(state["end_time"])
        except (KeyError, TypeError, ValueError):
            logger.warning("Corrupted giveaway end_time on restore; cancelling giveaway.")
            state["active"] = False
            state["ended"] = True
            await self.save_state(state)
            return

        remaining = (end_time - self.now()).total_seconds()
        if remaining <= 0:
            logger.info("Giveaway expired while bot was offline. Ending it now.")
            await self.end_giveaway()
        else:
            logger.info("Restored active giveaway. %.0f seconds remaining.", remaining)
            self._schedule_end(remaining)

    async def end_giveaway(self):
        """Pick a winner (if any), announce it, notify admin, and close the giveaway."""
        state = await self.get_state()
        if state.get("ended") and not state.get("active"):
            return  # already ended, nothing to do

        participants = await self.get_participants()

        if not participants:
            await self._safe_send(
                GIVEAWAY_CHANNEL_ID,
                "🎉 <b>GIVEAWAY ENDED</b>\n\nNo one joined this giveaway, so no winner could be picked. 😕",
            )
            await self._notify_admin("The giveaway ended with <b>no participants</b>.")
        else:
            winner_id_str = random.choice(list(participants.keys()))
            winner = participants[winner_id_str]
            display = self.display_name(winner.get("username"), winner.get("full_name", "Unknown"))

            mention = (
                f"@{winner['username']}"
                if winner.get("username")
                else f'<a href="tg://user?id={winner_id_str}">{html.escape(winner.get("full_name", "Winner"))}</a>'
            )

            channel_text = (
                "🎉 <b>GIVEAWAY ENDED</b>\n\n"
                "🏆 <b>Winner:</b>\n"
                f"{mention}\n\n"
                "Congratulations! 🎊"
            )
            await self._safe_send(GIVEAWAY_CHANNEL_ID, channel_text)

            admin_text = (
                "🏆 <b>Giveaway Winner</b>\n\n"
                f"Username: {display}\n"
                f"ID: <code>{winner_id_str}</code>\n"
                f"Full Name: {html.escape(winner.get('full_name', 'Unknown'))}\n"
                f"Total Participants: {len(participants)}"
            )
            await self._notify_admin(admin_text)

            state["winner_id"] = winner_id_str

        state["active"] = False
        state["ended"] = True
        await self.save_state(state)
        logger.info("Giveaway ended and archived.")

    # --------------------------------------------------------- participants

    async def is_participant(self, user_id: int) -> bool:
        participants = await self.get_participants()
        return str(user_id) in participants

    async def add_participant(self, user_id: int, username: Optional[str], full_name: str) -> int:
        """Register a participant. Returns the new total participant count."""
        participants = await self.get_participants()
        now = self.now()
        participants[str(user_id)] = {
            "user_id": user_id,
            "username": username,
            "full_name": full_name,
            "joined_at": now.isoformat(),
        }
        await self.save_participants(participants)
        return len(participants)

    async def participant_count(self) -> int:
        return len(await self.get_participants())

    # ------------------------------------------------------------ helpers

    async def _safe_send(self, chat_id, text: str, reply_markup=None):
        try:
            await self.bot.send_message(chat_id, text, reply_markup=reply_markup)
        except TelegramForbiddenError:
            logger.error("Bot is not allowed to post in %s (check admin rights).", chat_id)
        except TelegramBadRequest as e:
            logger.error("Bad request sending message to %s: %s", chat_id, e)
        except TelegramAPIError:
            logger.exception("Telegram API error while sending message to %s", chat_id)

    async def _notify_admin(self, text: str):
        await self._safe_send(ADMIN_ID, text)

    async def check_missing_channels(self, user_id: int) -> list:
        """Return the list of required-channel dicts the user has NOT joined."""
        missing = []
        for channel in REQUIRED_CHANNELS:
            try:
                member = await self.bot.get_chat_member(channel["chat_id"], user_id)
                if member.status not in (
                    ChatMemberStatus.MEMBER,
                    ChatMemberStatus.ADMINISTRATOR,
                    ChatMemberStatus.CREATOR,
                ):
                    missing.append(channel)
            except TelegramBadRequest:
                # e.g. user never interacted with that chat, or chat not found
                missing.append(channel)
            except TelegramForbiddenError:
                logger.error(
                    "Bot lacks permission to check membership in %s. Is it an admin there?",
                    channel["chat_id"],
                )
                missing.append(channel)
            except TelegramAPIError:
                logger.exception("Error checking membership in %s", channel["chat_id"])
                missing.append(channel)
        return missing


# ============================================================================
# FILTERS
# ============================================================================

class IsAdmin(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return message.from_user is not None and message.from_user.id == ADMIN_ID


# ============================================================================
# KEYBOARDS
# ============================================================================

def build_channel_post_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🎯 Join Giveaway", callback_data="join_giveaway")
    builder.adjust(1)
    return builder.as_markup()


def build_missing_channels_keyboard(missing_channels: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for channel in missing_channels:
        builder.button(text=f"📢 Join {channel['title']}", url=channel["url"])
    builder.button(text="🔄 Check Again", callback_data="check_again")
    builder.adjust(1)
    return builder.as_markup()


# ============================================================================
# ROUTER & HANDLERS
# ============================================================================

router = Router()
manager: Optional[GiveawayManager] = None  # set in main()


def fmt_dt(dt: datetime) -> tuple:
    return dt.strftime("%d.%m.%Y"), dt.strftime("%H:%M:%S")


def fmt_remaining(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


# --------------------------------------------------------------- /start

@router.message(Command("start"))
async def cmd_start(message: Message):
    if message.from_user and message.from_user.id == ADMIN_ID:
        text = (
            "👋 Welcome, Admin!\n\n"
            "<b>Available commands:</b>\n"
            "/giveaway 3h — start a giveaway (3h / 10m / 30m ...)\n"
            "/cancel — cancel the active giveaway\n"
            "/status — show giveaway status\n"
            "/participants — export the participants list"
        )
    else:
        text = "👋 Hello! Join the giveaway channel and press the giveaway button to participate."
    await message.answer(text)


# --------------------------------------------------------------- /giveaway

@router.message(Command("giveaway"), IsAdmin())
async def cmd_giveaway(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "⚠️ Usage: <code>/giveaway 3h</code>, <code>/giveaway 10m</code>, <code>/giveaway 30m</code>"
        )
        return

    duration_text = args[1].strip()
    seconds = GiveawayManager.parse_duration(duration_text)
    if seconds is None:
        await message.answer(
            "⚠️ Invalid duration format. Use something like <code>3h</code>, <code>10m</code>, or <code>30m</code>."
        )
        return

    state = await manager.start_giveaway(duration_text, seconds)

    channel_text = (
        "🎉 <b>GIVEAWAY STARTED</b>\n\n"
        "Press the button below to join the giveaway.\n\n"
        f"⏳ Ends in: {fmt_remaining(seconds)}"
    )

    try:
        await message.bot.send_message(
            GIVEAWAY_CHANNEL_ID,
            channel_text,
            reply_markup=build_channel_post_keyboard(),
        )
    except TelegramForbiddenError:
        await message.answer(
            "❌ I couldn't post in the giveaway channel. "
            "Make sure the bot is an admin there and the channel ID is correct."
        )
        return
    except TelegramBadRequest as e:
        await message.answer(f"❌ Failed to post in the giveaway channel: {e}")
        return

    await message.answer(
        f"✅ Giveaway started for <b>{duration_text}</b>. Any previous giveaway has been cancelled."
    )


# --------------------------------------------------------------- /cancel

@router.message(Command("cancel"), IsAdmin())
async def cmd_cancel(message: Message):
    cancelled = await manager.cancel_giveaway()
    if cancelled:
        try:
            await message.bot.send_message(
                GIVEAWAY_CHANNEL_ID,
                "🚫 <b>The current giveaway has been cancelled by the admin.</b>",
            )
        except TelegramAPIError:
            logger.exception("Could not announce cancellation in the channel.")
        await message.answer("✅ Giveaway cancelled.")
    else:
        await message.answer("ℹ️ There is no active giveaway to cancel.")


# --------------------------------------------------------------- /status

@router.message(Command("status"), IsAdmin())
async def cmd_status(message: Message):
    state = await manager.get_state()
    count = await manager.participant_count()

    if not state.get("active"):
        await message.answer(f"📊 <b>No active giveaway.</b>\n👥 Last recorded participants: {count}")
        return

    end_time = datetime.fromisoformat(state["end_time"])
    remaining = (end_time - manager.now()).total_seconds()

    text = (
        "📊 <b>Giveaway Status</b>\n\n"
        f"Active: ✅ Yes\n"
        f"Duration: {state.get('duration_text')}\n"
        f"Ends in: {fmt_remaining(remaining)}\n"
        f"👥 Participants: {count}"
    )
    await message.answer(text)


# --------------------------------------------------------------- /participants

@router.message(Command("participants"), IsAdmin())
async def cmd_participants(message: Message):
    participants = await manager.get_participants()

    if not participants:
        await message.answer("ℹ️ No participants yet.")
        return

    lines = []
    for data in participants.values():
        try:
            joined_dt = datetime.fromisoformat(data["joined_at"])
            joined_display = joined_dt.strftime("%d.%m.%Y %H:%M")
        except (KeyError, ValueError):
            joined_display = "unknown"

        name = f"@{data['username']}" if data.get("username") else data.get("full_name", "Unknown")
        lines.append(f"{name} - {joined_display}")

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PARTICIPANTS_TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    await message.answer_document(
        FSInputFile(PARTICIPANTS_TXT),
        caption=f"👥 Total participants: {len(participants)}",
    )


# --------------------------------------------------------------- join flow

async def _build_status_message(missing_channels: list, count: int) -> tuple:
    """Return (text, keyboard) for the 'not joined all channels' state."""
    text = (
        "❌ You haven't joined all required channels yet.\n\n"
        f"👥 Participants: {count}"
    )
    return text, build_missing_channels_keyboard(missing_channels)


@router.callback_query(F.data == "join_giveaway")
async def cb_join_giveaway(callback: CallbackQuery):
    await _handle_join_attempt(callback, edit_existing=False)


@router.callback_query(F.data == "check_again")
async def cb_check_again(callback: CallbackQuery):
    await _handle_join_attempt(callback, edit_existing=True)


async def _handle_join_attempt(callback: CallbackQuery, edit_existing: bool):
    state = await manager.get_state()

    if not state.get("active"):
        await callback.answer("This giveaway is no longer active.", show_alert=True)
        return

    user = callback.from_user
    full_name = user.full_name or "Unknown"

    # Already joined?
    if await manager.is_participant(user.id):
        count = await manager.participant_count()
        text = f"✅ You have already joined this giveaway.\n\n👥 Participants: {count}"
        await _deliver(callback, text, keyboard=None, edit_existing=edit_existing)
        await callback.answer()
        return

    missing = await manager.check_missing_channels(user.id)

    if missing:
        count = await manager.participant_count()
        text, keyboard = await _build_status_message(missing, count)
        await _deliver(callback, text, keyboard, edit_existing=edit_existing)
        await callback.answer()
        return

    # All channels joined -> register participant
    count = await manager.add_participant(user.id, user.username, full_name)
    text = f"✅ Successfully joined.\n\n👥 Participants: {count}"
    await _deliver(callback, text, keyboard=None, edit_existing=edit_existing)
    await callback.answer()

    # Notify admin privately
    date_str, time_str = fmt_dt(manager.now())
    display = manager.display_name(user.username, full_name)
    admin_text = (
        "🆕 <b>New Participant</b>\n\n"
        f"Username: {display}\n"
        f"ID: <code>{user.id}</code>\n"
        f"Full Name: {html.escape(full_name)}\n"
        f"Date: {date_str}\n"
        f"Time: {time_str}"
    )
    await manager._notify_admin(admin_text)


async def _deliver(callback: CallbackQuery, text: str, keyboard, edit_existing: bool):
    """
    Send the status text either by editing the bot's previous DM (check_again,
    since that message belongs to the user's private chat with the bot) or by
    sending a brand-new DM (initial join click from the channel post).
    """
    try:
        if edit_existing:
            await callback.message.edit_text(text, reply_markup=keyboard)
        else:
            await callback.bot.send_message(callback.from_user.id, text, reply_markup=keyboard)
    except TelegramForbiddenError:
        # The user has never started a private chat with the bot.
        await callback.answer(
            "⚠️ Please start a private chat with me first, then press Join Giveaway again.",
            show_alert=True,
        )
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            pass  # harmless — content was identical
        else:
            logger.exception("Failed to deliver status message to user.")
    except TelegramAPIError:
        logger.exception("Unexpected Telegram error while delivering status message.")


# ============================================================================
# STARTUP / MAIN
# ============================================================================

async def on_startup(bot: Bot):
    os.makedirs(DATA_DIR, exist_ok=True)
    await manager.restore_on_startup()
    logger.info("Bot startup complete. Data restored from JSON files.")


async def main():
    global manager

    if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        logger.error("BOT_TOKEN is not configured. Set it via .env or edit the script.")
        return

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    manager = GiveawayManager(bot)

    dp.startup.register(on_startup)

    logger.info("Starting Telegram Giveaway Bot...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
