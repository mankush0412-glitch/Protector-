import os
import logging
import asyncio
import random
import string
import datetime
import json
import hmac
import hashlib
import time
from urllib.parse import parse_qsl, quote
from contextlib import asynccontextmanager
from pymongo import MongoClient, ReturnDocument
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.templating import Jinja2Templates
import uvicorn

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, ChatMember
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    MessageHandler, filters, CallbackQueryHandler
)
from telegram.constants import ParseMode
from telegram.error import BadRequest

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

MONGODB_URI = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URL")
if not MONGODB_URI:
    raise Exception("MONGODB_URI/MONGO_URL environment variable not set!")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("BOT_TOKEN")
ADMIN_ID = os.environ.get("ADMIN_ID") or os.environ.get("ADMIN_USER_ID") or "0"
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
BRAND_TITLE = os.environ.get("BRAND_TITLE", "TEAM LEADER")
START_LECTURE_URL = os.environ.get(
    "START_LECTURE_URL",
    "https://telegra.ph/𝗟𝗘𝗖𝗧𝗨𝗥𝗘𝗦-𝗟𝗜𝗦𝗧-03-24-2",
)
LECTURE_LABEL = os.environ.get("LECTURE_LABEL", "Other Lectures 👇")
MONETAG_ZONE_ID = os.environ.get("MONETAG_ZONE_ID", "11609595")
AD_JOIN_FREQUENCY = max(1, int(os.environ.get("AD_JOIN_FREQUENCY", "2")))
AD_MIN_SECONDS = max(1, int(os.environ.get("AD_MIN_SECONDS", "8")))


def telegram_share_url(url: str, text: str = "📚 Other Lectures") -> str:
    """Build Telegram's native share flow without a duplicate WebApp share."""
    return (
        "https://t.me/share/url?url="
        f"{quote(url, safe='')}&text={quote(text, safe='')}"
    )

client = MongoClient(MONGODB_URI)
db_name = "protected_bot_db"
db = client[db_name]
links_collection = db["protected_links"]
users_collection = db["users"]
broadcast_collection = db["broadcast_history"]
channels_collection = db["channels"]
lectures_collection = db["lectures"]
ad_sessions_collection = db["ad_sessions"]
lectures_collection.create_index("created_at")


def init_db():
    try:
        client.admin.command('ismaster')
        logger.info("✅ MongoDB connected")
        users_collection.create_index("user_id", unique=True)
        links_collection.create_index("created_by")
        links_collection.create_index("active")
        channels_collection.create_index("channel_id", unique=True)
        logger.info("✅ Database indexes created")
    except Exception as e:
        logger.error(f"❌ MongoDB error: {e}")
        raise


def get_support_channels():
    return [item["id"] for item in get_support_channel_configs()]


def get_support_channel_configs():
    raw_config = os.environ.get("SUPPORT_CHANNELS_CONFIG", "").strip()
    if raw_config:
        try:
            configured = json.loads(raw_config)
            if isinstance(configured, list):
                channels = []
                for index, item in enumerate(configured):
                    if isinstance(item, str):
                        item = {"id": item}
                    if not isinstance(item, dict) or not item.get("id"):
                        continue
                    channel_id = str(item["id"]).strip()
                    channels.append({
                        "id": channel_id,
                        "name": str(item.get("name") or channel_id),
                        "image": str(item.get("image") or ""),
                        "url": str(item.get("url") or ""),
                        "index": index,
                    })
                if channels:
                    return channels
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Invalid SUPPORT_CHANNELS_CONFIG: %s", exc)

    raw = (
        os.environ.get("SUPPORT_CHANNELS")
        or os.environ.get("SUPPORT_CHANNEL_ID", "")
    ).strip()
    return [
        {
            "id": channel.strip(),
            "name": channel.strip(),
            "image": "",
            "url": "",
            "index": index,
        }
        for index, channel in enumerate(raw.split(","))
        if channel.strip()
    ]


def get_primary_support_channel():
    channels = get_support_channels()
    return channels[0] if channels else ""


async def get_channel_invite_link(context, channel_id: str) -> str:
    try:
        channel_data = channels_collection.find_one({"channel_id": channel_id})
        if channel_data and channel_data.get("invite_link"):
            if channel_data.get("created_at") and \
               (datetime.datetime.now() - channel_data["created_at"]).days < 1:
                return channel_data["invite_link"]

        try:
            chat_id = int(channel_id)
        except ValueError:
            chat_id = channel_id if channel_id.startswith('@') else f"@{channel_id}"

        try:
            bot = context.bot if hasattr(context, 'bot') else context
            invite_link = await bot.create_chat_invite_link(
                chat_id=chat_id,
                creates_join_request=True,
                name="Bot Access Link",
            )
            invite_url = invite_link.invite_link
            channels_collection.update_one(
                {"channel_id": channel_id},
                {"$set": {
                    "invite_link": invite_url,
                    "created_at": datetime.datetime.now(),
                    "last_updated": datetime.datetime.now()
                }},
                upsert=True
            )
            return invite_url
        except BadRequest:
            try:
                bot = context.bot if hasattr(context, 'bot') else context
                chat = await bot.get_chat(chat_id)
                if chat.invite_link:
                    return chat.invite_link
                elif chat.username:
                    return f"https://t.me/{chat.username}"
            except Exception:
                pass

            if str(channel_id).startswith('-100'):
                return f"https://t.me/c/{str(channel_id)[4:]}"
            elif str(channel_id).startswith('@'):
                return f"https://t.me/{str(channel_id)[1:]}"
            else:
                return f"https://t.me/{channel_id}"
    except Exception as e:
        logger.error(f"❌ Error getting channel invite link: {e}")
        if str(channel_id).startswith('-100'):
            return f"https://t.me/c/{str(channel_id)[4:]}"
        elif str(channel_id).startswith('@'):
            return f"https://t.me/{str(channel_id)[1:]}"
        else:
            return f"https://t.me/{channel_id}"


async def check_channel_membership(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    channels = get_support_channels()
    if not channels:
        return True

    async def is_member(support_channel):
        try:
            try:
                chat_id = int(support_channel)
            except ValueError:
                chat_id = support_channel if support_channel.startswith("@") else f"@{support_channel}"
            chat_member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            return chat_member.status in (ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER)
        except Exception as e:
            logger.error(f"❌ Channel check error ({support_channel}): {e}")
            return False

    results = await asyncio.gather(*[is_member(ch) for ch in channels])
    return all(results)


telegram_bot_app = Application.builder().token(TELEGRAM_TOKEN).build()


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


async def authenticated_webapp_user(request: Request):
    """Validate Telegram WebApp initData before any membership or unlock call."""
    init_data = request.headers.get("X-TG-Init-Data", "")
    if not init_data or not TELEGRAM_TOKEN:
        raise HTTPException(status_code=401, detail="Open this link inside Telegram")

    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", "")
    if not received_hash:
        raise HTTPException(status_code=401, detail="Invalid Telegram session")

    check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(values.items())
    )
    secret_key = hmac.new(
        b"WebAppData", TELEGRAM_TOKEN.encode(), hashlib.sha256
    ).digest()
    expected_hash = hmac.new(
        secret_key, check_string.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(status_code=401, detail="Invalid Telegram session")

    try:
        auth_date = int(values.get("auth_date", "0"))
        if time.time() - auth_date > 86400:
            raise HTTPException(status_code=401, detail="Telegram session expired")
        user_data = json.loads(values["user"])
        user_id = int(user_data["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="Invalid Telegram user data")

    users_collection.update_one(
        {"user_id": user_id},
        {"$set": {
            "username": user_data.get("username"),
            "first_name": user_data.get("first_name"),
            "last_active": datetime.datetime.now(),
        }},
        upsert=True,
    )
    return user_data


def get_link_or_404(token):
    link = links_collection.find_one({"_id": token, "active": True})
    if not link:
        raise HTTPException(status_code=404, detail="Link expired or revoked")
    return link


async def channel_is_member(user_id, channel_id):
    try:
        try:
            chat_id = int(channel_id)
        except (TypeError, ValueError):
            chat_id = channel_id if str(channel_id).startswith("@") else f"@{channel_id}"
        member = await telegram_bot_app.bot.get_chat_member(
            chat_id=chat_id, user_id=user_id
        )
        return member.status in (
            ChatMember.MEMBER,
            ChatMember.ADMINISTRATOR,
            ChatMember.OWNER,
        )
    except Exception as exc:
        logger.warning("WebApp membership check failed for %s: %s", channel_id, exc)
        return False


async def build_join_state(token: str, user_id=None):
    if token == "home":
        return {
            "home": True,
            "brandTitle": BRAND_TITLE,
            "lectureUrl": START_LECTURE_URL,
            "lectureLabel": LECTURE_LABEL,
            "channels": [],
            "allJoined": True,
        }

    get_link_or_404(token)
    configs = get_support_channel_configs()
    if user_id is None:
        raise HTTPException(status_code=401, detail="Telegram user required")

    memberships = await asyncio.gather(*[
        channel_is_member(user_id, item["id"]) for item in configs
    ])
    channels = []
    for item, joined in zip(configs, memberships):
        join_url = item["url"] or await get_channel_invite_link(
            telegram_bot_app.bot, item["id"]
        )
        channels.append({
            "index": item["index"],
            "id": item["id"],
            "name": item["name"],
            "image": item["image"],
            "joinUrl": join_url,
            "joined": bool(joined),
        })

    joined_count = sum(1 for item in channels if item["joined"])
    return {
        "home": False,
        "brandTitle": BRAND_TITLE,
        "channels": channels,
        "joinedCount": joined_count,
        "total": len(channels),
        "allJoined": joined_count == len(channels),
    }


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    users_collection.update_one(
        {"user_id": user_id},
        {"$set": {
            "username": update.effective_user.username,
            "first_name": update.effective_user.first_name,
            "last_active": datetime.datetime.now()
        }},
        upsert=True
    )

    if context.args:
        token = context.args[0]
        link_data = links_collection.find_one({"_id": token, "active": True})
        if link_data:
            web_app_url = f"{RENDER_URL}/join?token={token}"
            keyboard = [[
                InlineKeyboardButton(
                    "🔐 Open Link", web_app=WebAppInfo(url=web_app_url)
                )
            ], [
                InlineKeyboardButton(
                    "📤 Share",
                    url=telegram_share_url(START_LECTURE_URL),
                )
            ]]
            await update.message.reply_text(
                f"💠 *{BRAND_TITLE}*\n\n"
                "🔐 Your protected link is ready.\n"
                "⚡ Tap below to continue with secure access.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await update.message.reply_text("❌ Link expired or revoked")
        return

    if not await check_channel_membership(user_id, context):
        callback_data = f"check_join_{context.args[0]}" if context.args else "check_join"

        async def get_button_for_channel(ch):
            try:
                try:
                    chat_id_check = int(ch)
                except ValueError:
                    chat_id_check = ch if ch.startswith("@") else f"@{ch}"
                member = await context.bot.get_chat_member(
                    chat_id=chat_id_check,
                    user_id=user_id
                )
                if member.status in (ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER):
                    return None
            except Exception:
                pass
            invite_link = await get_channel_invite_link(context, ch)
            return InlineKeyboardButton("📢 𝙅𝙤𝙞𝙣 𝘾𝙝𝙖𝙣𝙣𝙚𝙡", url=invite_link)

        results = await asyncio.gather(*[get_button_for_channel(ch) for ch in get_support_channels()])
        channel_buttons = [btn for btn in results if btn is not None]

        keyboard = []
        for i in range(0, len(channel_buttons), 2):
            keyboard.append(channel_buttons[i:i+2])

        if not channel_buttons:
            keyboard.append([InlineKeyboardButton("✅ 𝘼𝙡𝙧𝙚𝙖𝙙𝙮 𝙅𝙤𝙞𝙣𝙚𝙙", callback_data=callback_data)])
        else:
            keyboard.append([InlineKeyboardButton("✅ 𝘾𝙝𝙚𝙘𝙠 𝙈𝙚𝙢𝙗𝙚𝙧𝙨𝙝𝙞𝙥", callback_data=callback_data)])

        await update.message.reply_text(
            "✨ 𝙒𝙀𝙇𝘾𝙊𝙈𝙀 𝙏𝙊 𝙏𝙀𝘼𝙈 𝙇𝙀𝘼𝘿𝙀𝙍 ✨\n\n"
            "🔐 𝙔𝙤𝙪𝙧 𝘼𝙘𝙘𝙚𝙨𝙨 𝙄𝙨 𝘽𝙚𝙞𝙣𝙜 𝙑𝙚𝙧𝙞𝙛𝙞𝙚𝙙...\n\n"
            "📢 𝙋𝙡𝙚𝙖𝙨𝙚 𝙅𝙤𝙞𝙣 𝘼𝙡𝙡 𝙍𝙚𝙦𝙪𝙞𝙧𝙚𝙙 𝘾𝙝𝙖𝙣𝙣𝙚𝙡𝙨\n"
            "🚀 𝙏𝙤 𝙐𝙣𝙡𝙤𝙘𝙠 𝙔𝙤𝙪𝙧 𝙋𝙧𝙤𝙩𝙚𝙘𝙩𝙚𝙙 𝙇𝙞𝙣𝙠\n\n"
            "👇 𝘾𝙤𝙢𝙥𝙡𝙚𝙩𝙚 𝙏𝙝𝙚 𝙎𝙩𝙚𝙥𝙨 𝘼𝙣𝙙 𝘾𝙡𝙞𝙘𝙠 𝘾𝙃𝙀𝘾𝙆",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    user_name = update.effective_user.first_name or "User"
    welcome_msg = (
        f"💎 *{BRAND_TITLE}* 💎\n\n"
        "🔐 Secure protected-link access\n"
        "⚡ Fast • Safe • Simple\n\n"
        "ʟᴏsᴛ ɪɴ ᴍʏsᴇʟғ\n"
        f"📚 *Other Lectures:* [{LECTURE_LABEL}]({START_LECTURE_URL})\n\n"
        "📢 *Available Commands:*\n"
        "➤ /start\n"
        "➤ /protect\n"
        "➤ /help\n\n"
        "🚀 Ready to generate your protected link!"
    )

    ch_btns_start = []
    for ch in get_support_channels():
        invite_link = await get_channel_invite_link(context, ch)
        ch_btns_start.append(InlineKeyboardButton("⭐ 𝙎𝙪𝙥𝙥𝙤𝙧𝙩 𝘾𝙝𝙖𝙣𝙣𝙚𝙡", url=invite_link))

    keyboard = []
    for i in range(0, len(ch_btns_start), 2):
        keyboard.append(ch_btns_start[i:i+2])

    keyboard.insert(0, [
        InlineKeyboardButton(
            "🔐 Open Link",
            web_app=WebAppInfo(url=f"{RENDER_URL}/join?token=home"),
        )
    ])
    keyboard.insert(1, [
        InlineKeyboardButton(
            "📤 Share",
            url=telegram_share_url(START_LECTURE_URL),
        )
    ])
    keyboard.append([
        InlineKeyboardButton("🚀 𝘾𝙧𝙚𝙖𝙩𝙚 𝙋𝙧𝙤𝙩𝙚𝙘𝙩𝙚𝙙 𝙇𝙞𝙣𝙠", callback_data="create_link")
    ])

    await update.message.reply_text(
        welcome_msg,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == "check_join":
        if await check_channel_membership(query.from_user.id, context):
            await query.message.edit_text(
                "✅ *Verification Successful*\n\n"
                "🎉 You have successfully joined all channels!\n"
                "🚀 You can now continue using the bot.",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await query.answer("❌ Not joined yet. Please join first.", show_alert=True)

    elif query.data.startswith("check_join_"):
        token = query.data.replace("check_join_", "")

        if await check_channel_membership(query.from_user.id, context):
            links_collection.update_one(
                {"_id": token},
                {"$addToSet": {"verified_users": query.from_user.id}}
            )

            link_data = links_collection.find_one({"_id": token, "active": True})

            if link_data:
                web_app_url = f"{RENDER_URL}/join?token={token}"
                keyboard = [[
                    InlineKeyboardButton("🔐 Open Link", web_app=WebAppInfo(url=web_app_url))
                ], [
                    InlineKeyboardButton(
                        "📤 Share",
                        url=telegram_share_url(START_LECTURE_URL),
                    )
                ]]
                await query.message.edit_text(
                    "💠 *ACCESS GRANTED*\n\n"
                    "🔓 Verification complete\n"
                    "🚀 Click below to open your link",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await query.message.edit_text("❌ Link expired or revoked")
        else:
            await query.answer("❌ Not joined yet. Please join first.", show_alert=True)

    elif query.data == "create_link":
        await query.message.reply_text(
            "🛠 *Create Protected Link*\n\n"
            "Use:\n/protect https://t.me/yourchannel\n\n"
            "Replace with your actual link.",
            parse_mode=ParseMode.MARKDOWN
        )

    elif query.data == "confirm_broadcast":
        await handle_broadcast_confirmation(update, context)

    elif query.data == "cancel_broadcast":
        await query.message.edit_text("❌ Broadcast cancelled")

    elif query.data.startswith("revoke_"):
        link_id = query.data.replace("revoke_", "")
        await handle_revoke_link(update, context, link_id)


async def protect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_channel_membership(update.effective_user.id, context):
        ch_btns_p = []
        for ch in get_support_channels():
            invite_link = await get_channel_invite_link(context, ch)
            ch_btns_p.append(InlineKeyboardButton("📢 Join Channel", url=invite_link))
        keyboard = []
        for i in range(0, len(ch_btns_p), 2):
            keyboard.append(ch_btns_p[i:i+2])
        keyboard.append([InlineKeyboardButton("✅ Check Membership", callback_data="check_join")])
        await update.message.reply_text(
            "🔐 Join our channel first to use this bot.\nThen click 'Check Membership' below.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if not context.args or not context.args[0].startswith("https://t.me/"):
        await update.message.reply_text(
            "Usage: `/protect https://t.me/yourchannel`\n\n"
            "This works for:\n"
            "• Channels (public/private)\n"
            "• Groups (public/private)\n"
            "• Supergroups",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    telegram_link = context.args[0]

    if not telegram_link.startswith("https://t.me/"):
        await update.message.reply_text("❌ Invalid link. Must start with https://t.me/")
        return

    token = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    short_id = token.upper()

    links_collection.insert_one({
        "_id": token,
        "short_id": short_id,
        "telegram_link": telegram_link,
        "link_type": "channel" if "/c/" in telegram_link or "/s/" in telegram_link else "group",
        "created_by": update.effective_user.id,
        "created_by_name": update.effective_user.first_name,
        "created_at": datetime.datetime.now(),
        "active": True,
        "clicks": 0
    })

    bot_info = await context.bot.get_me()
    bot_username = bot_info.username
    protected_link = f"https://t.me/{bot_username}?start={token}"

    keyboard = [[
        InlineKeyboardButton("📤 Share", url=f"https://t.me/share/url?url={protected_link}&text=🔐 Protected Link"),
        InlineKeyboardButton("❌ Revoke", callback_data=f"revoke_{token}")
    ]]

    await update.message.reply_text(
        f"✅ *Protected Link Created!*\n\n"
        f"🔑 *Link ID:* `{short_id}`\n"
        f"📊 *Status:* 🟢 Active\n"
        f"🔗 *Original Link:* `{telegram_link}`\n"
        f"📝 *Type:* {'Channel' if '/c/' in telegram_link else 'Group'}\n"
        f"⏰ *Created:* {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        f"🔐 *Your Protected Link:*\n"
        f"`{protected_link}`\n\n"
        f"📋 *Quick Actions:*\n"
        f"• Copy the link above\n"
        f"• Share with your audience\n"
        f"• Revoke anytime with `/revoke {short_id}`",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )


async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_channel_membership(update.effective_user.id, context):
        ch_btns_r = []
        for ch in get_support_channels():
            invite_link = await get_channel_invite_link(context, ch)
            ch_btns_r.append(InlineKeyboardButton("📢 Join Channel", url=invite_link))
        keyboard = []
        for i in range(0, len(ch_btns_r), 2):
            keyboard.append(ch_btns_r[i:i+2])
        keyboard.append([InlineKeyboardButton("✅ Check Membership", callback_data="check_join")])
        await update.message.reply_text(
            "🔐 Join our channel first to use this bot.\nThen click 'Check Membership' below.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if not context.args:
        user_id = update.effective_user.id
        active_links = list(links_collection.find(
            {"created_by": user_id, "active": True},
            sort=[("created_at", -1)],
            limit=10
        ))

        if not active_links:
            await update.message.reply_text("📭 No active links")
            return

        message = "🔐 *Your Active Links:*\n\n"
        keyboard = []

        for link in active_links:
            short_id = link.get('short_id', link['_id'][:8])
            clicks = link.get('clicks', 0)
            created = link.get('created_at', datetime.datetime.now()).strftime('%m/%d')
            message += f"• `{short_id}` - {clicks} clicks - {created}\n"
            keyboard.append([InlineKeyboardButton(
                f"❌ Revoke {short_id}",
                callback_data=f"revoke_{link['_id']}"
            )])

        message += "\nClick a button below to revoke."
        await update.message.reply_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    link_id = context.args[0].upper()
    query_filter = {
        "$or": [
            {"short_id": link_id},
            {"_id": link_id.lower()}
        ],
        "created_by": update.effective_user.id,
        "active": True
    }

    link_data = links_collection.find_one(query_filter)

    if not link_data:
        await update.message.reply_text("❌ Link not found")
        return

    links_collection.update_one(
        {"_id": link_data['_id']},
        {"$set": {"active": False, "revoked_at": datetime.datetime.now()}}
    )

    await update.message.reply_text(
        f"✅ *Link Revoked!*\n\n"
        f"Link `{link_data.get('short_id', link_id)}` has been permanently revoked.\n\n"
        f"⚠️ All future access attempts will be blocked.",
        parse_mode=ParseMode.MARKDOWN
    )


async def handle_revoke_link(update: Update, context: ContextTypes.DEFAULT_TYPE, link_id: str):
    query = update.callback_query

    link_data = links_collection.find_one({"_id": link_id, "active": True})

    if not link_data:
        await query.message.edit_text(
            "❌ Link not found or already revoked.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    if link_data['created_by'] != query.from_user.id:
        await query.message.edit_text(
            "❌ You don't have permission to revoke this link.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    links_collection.update_one(
        {"_id": link_id},
        {"$set": {"active": False, "revoked_at": datetime.datetime.now()}}
    )

    await query.message.edit_text(
        f"✅ *Link Revoked!*\n\n"
        f"Link `{link_data.get('short_id', link_id[:8])}` has been revoked.\n"
        f"👥 Final Clicks: {link_data.get('clicks', 0)}\n\n"
        f"⚠️ All access has been permanently blocked.",
        parse_mode=ParseMode.MARKDOWN
    )


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admin_id = int(ADMIN_ID)
    if update.effective_user.id != admin_id:
        await update.message.reply_text(
            "🔒 *Admin Access Required*\n\n"
            "This command is restricted to administrators only.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    if not update.message.reply_to_message:
        await update.message.reply_text(
            "📢 *Broadcast System*\n\n"
            "To broadcast a message:\n"
            "1. Send any message\n"
            "2. Reply to it with `/broadcast`\n"
            "3. Confirm the action\n\n"
            "✨ *Features:*\n"
            "• Supports all media types\n"
            "• Preserves formatting\n"
            "• Tracks delivery\n"
            "• No rate limiting",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    total_users = users_collection.count_documents({})
    keyboard = [
        [InlineKeyboardButton("✅ Confirm Broadcast", callback_data="confirm_broadcast")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_broadcast")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    content_type = getattr(update.message.reply_to_message, 'content_type', 'text')

    await update.message.reply_text(
        f"⚠️ *Broadcast Confirmation*\n\n"
        f"📊 *Delivery Stats:*\n"
        f"• 📨 Recipients: `{total_users}` users\n"
        f"• 📝 Type: {content_type}\n"
        f"• ⚡ Delivery: Instant\n\n"
        f"Are you sure you want to proceed?",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

    context.user_data['broadcast_message'] = update.message.reply_to_message


async def handle_broadcast_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    await query.message.edit_text(
        "📤 *Broadcasting...*\n\nPlease wait, this may take a moment.",
        parse_mode=ParseMode.MARKDOWN
    )

    users = list(users_collection.find({}))
    total_users = len(users)
    successful = 0
    failed = 0

    message_to_broadcast = context.user_data.get('broadcast_message')

    if not message_to_broadcast:
        await query.message.edit_text("❌ No message found. Reply to a message and use /broadcast.")
        return

    for user in users:
        try:
            await message_to_broadcast.copy(chat_id=user['user_id'])
            successful += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Failed: {user['user_id']}: {e}")
            failed += 1

    broadcast_collection.insert_one({
        "admin_id": query.from_user.id,
        "date": datetime.datetime.now(),
        "total_users": total_users,
        "successful": successful,
        "failed": failed
    })

    success_rate = (successful / total_users * 100) if total_users > 0 else 0

    await query.message.edit_text(
        f"✅ *Broadcast Complete!*\n\n"
        f"📊 *Delivery Report:*\n"
        f"• 📨 Total Recipients: `{total_users}`\n"
        f"• ✅ Successful: `{successful}`\n"
        f"• ❌ Failed: `{failed}`\n"
        f"• 📈 Success Rate: `{success_rate:.1f}%`\n"
        f"• ⏰ Time: {datetime.datetime.now().strftime('%H:%M:%S')}\n\n"
        f"✨ Broadcast logged in system.",
        parse_mode=ParseMode.MARKDOWN
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admin_id = int(ADMIN_ID)
    if update.effective_user.id != admin_id:
        await update.message.reply_text(
            "🔒 *Admin Access Required*\n\n"
            "This command is restricted to administrators only.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    total_users = users_collection.count_documents({})
    total_links = links_collection.count_documents({})
    active_links = links_collection.count_documents({"active": True})

    today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    new_users_today = users_collection.count_documents({"last_active": {"$gte": today}})
    new_links_today = links_collection.count_documents({"created_at": {"$gte": today}})

    total_clicks_result = list(links_collection.aggregate([
        {"$group": {"_id": None, "total_clicks": {"$sum": "$clicks"}}}
    ]))
    total_clicks = total_clicks_result[0].get('total_clicks', 0) if total_clicks_result else 0

    await update.message.reply_text(
        f"📊 *System Analytics Dashboard*\n\n"
        f"👥 *User Statistics*\n"
        f"• 📈 Total Users: `{total_users}`\n"
        f"• 🆕 New Today: `{new_users_today}`\n\n"
        f"🔗 *Link Statistics*\n"
        f"• 🔢 Total Links: `{total_links}`\n"
        f"• 🟢 Active Links: `{active_links}`\n"
        f"• 🆕 Created Today: `{new_links_today}`\n"
        f"• 👆 Total Clicks: `{total_clicks}`\n\n"
        f"⚙️ *System Status*\n"
        f"• 🗄️ Database: 🟢 Operational\n"
        f"• 🤖 Bot: 🟢 Online\n"
        f"• ⚡ Uptime: 100%\n"
        f"• 🕐 Last Update: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        parse_mode=ParseMode.MARKDOWN
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if not await check_channel_membership(user_id, context):
        ch_btns_h = []
        for ch in get_support_channels():
            invite_link = await get_channel_invite_link(context, ch)
            ch_btns_h.append(InlineKeyboardButton("📢 Join Channel", url=invite_link))
        keyboard = []
        for i in range(0, len(ch_btns_h), 2):
            keyboard.append(ch_btns_h[i:i+2])
        keyboard.append([InlineKeyboardButton("✅ Check Membership", callback_data="check_join")])
        await update.message.reply_text(
            "🔐 Join our channel first to use this bot.\nThen click 'Check Membership' below.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    ch_btns_help = []
    for ch in get_support_channels():
        invite_link = await get_channel_invite_link(context, ch)
        ch_btns_help.append(InlineKeyboardButton("⭐ Support Channel", url=invite_link))
    keyboard = []
    for i in range(0, len(ch_btns_help), 2):
        keyboard.append(ch_btns_help[i:i+2])

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

    await update.message.reply_text(
        "🛡️ *LinkShield Pro - Help Center*\n\n"
        "✨ *What I Can Protect:*\n"
        "• 🔗 Telegram Channels\n"
        "• 👥 Telegram Groups\n"
        "• 🛡️ Private/Public links\n"
        "• 🔒 Supergroups\n\n"
        "📋 *Available Commands:*\n"
        "• `/start` - Start the bot\n"
        "• `/protect https://t.me/channel` - Create secure link\n"
        "• `/revoke` - Revoke access\n"
        "• `/help` - This message\n\n"
        "🔒 *How to Use:*\n"
        "1. Use `/protect https://t.me/yourchannel`\n"
        "2. Share the generated link\n"
        "3. Users join via verification\n"
        "4. Manage with `/revoke`\n\n"
        "💡 *Pro Tips:*\n"
        "• Works with any t.me link\n"
        "• Monitor link analytics\n"
        "• Revoke unused links\n"
        "• Join our support channel",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = int(ADMIN_ID)

    if update.effective_user.id != admin_id:
        await update.message.reply_text("❌ Only admin can add lectures.")
        return

    if not update.message.reply_to_message:
        await update.message.reply_text(
            "⚠️ Kisi message pe reply karke /add likho",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    content = (
        update.message.reply_to_message.text
        or update.message.reply_to_message.caption
    )

    if not content:
        await update.message.reply_text("❌ Empty content add nahi ho sakta.")
        return

    lectures_collection.insert_one({
        "content": content,
        "created_at": datetime.datetime.now(),
        "added_by": update.effective_user.id
    })

    await update.message.reply_text("✅ Lecture successfully added!")


async def lecture_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lectures = list(lectures_collection.find().sort("created_at", 1))

    if not lectures:
        await update.message.reply_text("📭 Abhi koi lecture add nahi hai.")
        return

    message = "📚 Lecture List\n\n"
    for i, lec in enumerate(lectures, start=1):
        message += f"{i}. {lec['content']}\n\n"

    await update.message.reply_text(
        message[:4096],
        disable_web_page_preview=True
    )


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = int(ADMIN_ID)

    if update.effective_user.id != admin_id:
        await update.message.reply_text("❌ Only admin can delete lectures.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "⚠️ Usage:\n/delete <lecture_number>\n\nExample:\n/delete 2",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    index = int(context.args[0]) - 1
    lectures = list(lectures_collection.find().sort("created_at", 1))

    if index < 0 or index >= len(lectures):
        await update.message.reply_text("❌ Invalid lecture number.")
        return

    lecture = lectures[index]
    lectures_collection.delete_one({"_id": lecture["_id"]})

    await update.message.reply_text(
        f"✅ Lecture *{index + 1}* deleted successfully!",
        parse_mode=ParseMode.MARKDOWN
    )


async def store_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.chat.type == "private":
        users_collection.update_one(
            {"user_id": update.effective_user.id},
            {"$set": {"last_active": update.message.date}},
            upsert=True
        )


telegram_bot_app.add_handler(CommandHandler("start", start))
telegram_bot_app.add_handler(CommandHandler("protect", protect_command))
telegram_bot_app.add_handler(CommandHandler("revoke", revoke_command))
telegram_bot_app.add_handler(CommandHandler("broadcast", broadcast_command))
telegram_bot_app.add_handler(CommandHandler("stats", stats_command))
telegram_bot_app.add_handler(CommandHandler("help", help_command))
telegram_bot_app.add_handler(CommandHandler("add", add_command))
telegram_bot_app.add_handler(CommandHandler("lecture", lecture_command))
telegram_bot_app.add_handler(CommandHandler("delete", delete_command))
telegram_bot_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, store_message))
telegram_bot_app.add_handler(CallbackQueryHandler(button_callback))


async def keep_alive():
    await asyncio.sleep(60)
    while True:
        try:
            await telegram_bot_app.bot.get_me()
            logger.info("✅ Keep-alive ping sent")
        except Exception as e:
            logger.warning(f"Keep-alive ping failed: {e}")
        await asyncio.sleep(270)


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    logger.info("Starting bot...")

    required_vars = {
        "TELEGRAM_TOKEN/BOT_TOKEN": TELEGRAM_TOKEN,
        "RENDER_EXTERNAL_URL": RENDER_URL,
        "MONGODB_URI/MONGO_URL": MONGODB_URI,
    }
    for var, value in required_vars.items():
        if not value:
            logger.critical(f"Missing env var: {var}")
            raise Exception(f"Missing required environment variable: {var}")

    init_db()

    await telegram_bot_app.initialize()
    await telegram_bot_app.start()

    render_url = RENDER_URL
    token_val = TELEGRAM_TOKEN
    webhook_url = f"{render_url}/{token_val}"

    try:
        await telegram_bot_app.bot.set_webhook(url=webhook_url)
        logger.info(f"✅ Webhook set: {webhook_url}")
    except Exception as e:
        logger.error(f"❌ Failed to set webhook: {e}")

    try:
        bot_info = await telegram_bot_app.bot.get_me()
        logger.info(f"✅ Bot: @{bot_info.username}")
    except Exception as e:
        logger.error(f"❌ Failed to get bot info: {e}")

    ping_task = asyncio.create_task(keep_alive())

    yield

    ping_task.cancel()
    logger.info("Stopping bot...")
    try:
        await telegram_bot_app.stop()
        await telegram_bot_app.shutdown()
    except Exception as e:
        logger.error(f"Shutdown error: {e}")
    client.close()
    logger.info("✅ Bot stopped")


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")


@app.post("/{token}")
async def telegram_webhook(request: Request, token: str):
    if token != TELEGRAM_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")

    update_data = await request.json()
    update = Update.de_json(update_data, telegram_bot_app.bot)
    await telegram_bot_app.process_update(update)
    return Response(status_code=200)


@app.get("/getgrouplink/{token}")
async def get_group_link(request: Request, token: str):
    await authenticated_webapp_user(request)
    get_link_or_404(token)
    return {
        "error": "Complete the final ad unlock first",
        "unlockRequired": True,
    }


@app.get("/api/join-state/{token}")
async def join_state(request: Request, token: str):
    user = await authenticated_webapp_user(request)
    return await build_join_state(token, int(user["id"]))


@app.post("/api/verify-channel/{token}/{channel_index}")
async def verify_channel(request: Request, token: str, channel_index: int):
    user = await authenticated_webapp_user(request)
    state = await build_join_state(token, int(user["id"]))
    if state.get("home"):
        raise HTTPException(status_code=400, detail="No channel verification on home")

    channel = next(
        (item for item in state["channels"] if item["index"] == channel_index),
        None,
    )
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    if not channel["joined"]:
        return {"verified": False, "adRequired": False, "state": state}

    progress_key = f"join_progress.{token}"
    progress_doc = users_collection.find_one(
        {"user_id": int(user["id"])},
        {progress_key: 1},
    ) or {}
    progress_root = progress_doc.get("join_progress", {})
    previous_progress = progress_root.get(token, []) if isinstance(progress_root, dict) else []
    if not isinstance(previous_progress, list):
        previous_progress = []
    is_new_verification = channel_index not in previous_progress
    users_collection.update_one(
        {"user_id": int(user["id"])},
        {"$addToSet": {progress_key: channel_index}},
        upsert=True,
    )
    ad_required = is_new_verification and (
        len(previous_progress) % AD_JOIN_FREQUENCY == 0
    )
    return {
        "verified": True,
        "adRequired": ad_required,
        "state": state,
    }


@app.post("/api/ad/start/{token}")
async def start_ad_session(request: Request, token: str):
    user = await authenticated_webapp_user(request)
    state = await build_join_state(token, int(user["id"]))
    if state.get("home") or not state["allJoined"]:
        raise HTTPException(status_code=403, detail="Verify all required channels first")

    ad_token = "".join(random.choices(string.ascii_letters + string.digits, k=40))
    now = utc_now()
    ad_sessions_collection.insert_one({
        "_id": ad_token,
        "user_id": int(user["id"]),
        "link_token": token,
        "started_at": now,
        "expires_at": now + datetime.timedelta(minutes=10),
        "claimed": False,
    })
    return {"adToken": ad_token, "minSeconds": AD_MIN_SECONDS}


@app.post("/api/ad/claim/{token}")
async def claim_ad_session(request: Request, token: str):
    user = await authenticated_webapp_user(request)
    payload = await request.json()
    ad_token = str(payload.get("adToken", ""))
    session = ad_sessions_collection.find_one({
        "_id": ad_token,
        "user_id": int(user["id"]),
        "link_token": token,
        "claimed": False,
    })
    if not session:
        raise HTTPException(status_code=400, detail="Ad session is invalid or already used")

    started_at = session["started_at"]
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=datetime.timezone.utc)
    elapsed = (utc_now() - started_at).total_seconds()
    if elapsed < AD_MIN_SECONDS:
        raise HTTPException(
            status_code=425,
            detail=f"Please keep the ad open for {AD_MIN_SECONDS - int(elapsed)} more seconds",
        )
    expires_at = session.get("expires_at")
    if expires_at:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)
        if expires_at < utc_now():
            raise HTTPException(status_code=400, detail="Ad session expired")

    state = await build_join_state(token, int(user["id"]))
    if not state["allJoined"]:
        raise HTTPException(status_code=403, detail="Channel verification is incomplete")

    claimed = ad_sessions_collection.find_one_and_update(
        {"_id": ad_token, "claimed": False},
        {"$set": {"claimed": True, "claimed_at": utc_now()}},
        return_document=ReturnDocument.AFTER,
    )
    if not claimed:
        raise HTTPException(status_code=400, detail="Ad session is already used")
    return {"url": get_link_or_404(token)["telegram_link"]}


@app.get("/join")
async def join_page(request: Request, token: str):
    return templates.TemplateResponse(
        "join.html",
        {
            "request": request,
            "token": token,
            "brand_title": BRAND_TITLE,
            "lecture_url": START_LECTURE_URL,
            "lecture_label": LECTURE_LABEL,
            "monetag_zone_id": MONETAG_ZONE_ID,
        },
    )


@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "LinkShield Pro",
        "version": "3.0.0",
        "time": datetime.datetime.now().isoformat()
    }


@app.get("/health")
async def health():
    try:
        client.admin.command("ping")
        mongo_status = "connected"
    except Exception:
        mongo_status = "error"
    return {"status": "ok", "mongo": mongo_status, "service": "LinkShield Pro"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
