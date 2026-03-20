# --- IMPORTS ---
import os
import logging
import asyncio
import random
import string
import datetime
from typing import Optional
from pymongo import MongoClient
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.templating import Jinja2Templates

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, ChatMember, ChatInviteLink
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

# --- LOGGING ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- DATABASE ---
MONGODB_URI = os.environ.get("MONGODB_URI")
if not MONGODB_URI:
    raise Exception("MONGODB_URI environment variable not set!")

client = MongoClient(MONGODB_URI)
db_name = "protected_bot_db"
db = client[db_name]

links_collection = db["protected_links"]
users_collection = db["users"]
broadcast_collection = db["broadcast_history"]
channels_collection = db["channels"]
lectures_collection = db["lectures"]

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
        # ================= MULTI SUPPORT =================
def get_support_channels():
    raw = os.environ.get("SUPPORT_CHANNELS", "").strip()
    if not raw:
        return []
    return [c.strip() for c in raw.split(",") if c.strip()]

def get_primary_support_channel():
    channels = get_support_channels()
    return channels[0] if channels else ""

# ================= INVITE LINK =================
async def get_channel_invite_link(context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> str:
    try:
        channel_data = channels_collection.find_one({"channel_id": channel_id})

        # 🔥 CACHE CHECK
        if channel_data and channel_data.get("invite_link"):
            if channel_data.get("created_at") and \
               (datetime.datetime.now() - channel_data["created_at"]).days < 1:
                return channel_data["invite_link"]

        try:
            chat_id = int(channel_id)
        except ValueError:
            chat_id = channel_id if channel_id.startswith('@') else f"@{channel_id}"

        # 🔥 CREATE INVITE
        try:
            invite_link = await context.bot.create_chat_invite_link(
                chat_id=chat_id,
                creates_join_request=True
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
            # fallback
            try:
                chat = await context.bot.get_chat(chat_id)
                if chat.invite_link:
                    return chat.invite_link
                elif chat.username:
                    return f"https://t.me/{chat.username}"
            except:
                pass

            # final fallback
            if str(channel_id).startswith('-100'):
                return f"https://t.me/c/{str(channel_id)[4:]}"
            elif str(channel_id).startswith('@'):
                return f"https://t.me/{str(channel_id)[1:]}"
            else:
                return f"https://t.me/{channel_id}"

    except Exception as e:
        logger.error(f"❌ Invite error: {e}")

        if str(channel_id).startswith('-100'):
            return f"https://t.me/c/{str(channel_id)[4:]}"
        elif str(channel_id).startswith('@'):
            return f"https://t.me/{str(channel_id)[1:]}"
        else:
            return f"https://t.me/{channel_id}"

# ================= MEMBERSHIP CHECK =================
async def check_channel_membership(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    channels = get_support_channels()

    if not channels:
        return True

    for support_channel in channels:
        try:
            try:
                chat_id = int(support_channel)
            except ValueError:
                chat_id = support_channel if support_channel.startswith("@") else f"@{support_channel}"

            chat_member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)

            if chat_member.status not in (
                ChatMember.MEMBER,
                ChatMember.ADMINISTRATOR,
                ChatMember.OWNER
            ):
                return False

        except Exception as e:
            logger.error(f"❌ Membership error ({support_channel}): {e}")
            return False

    return True
    # ================= START =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    # SAVE USER
    users_collection.update_one(
        {"user_id": user_id},
        {"$set": {
            "username": update.effective_user.username,
            "first_name": update.effective_user.first_name,
            "last_active": datetime.datetime.now()
        }},
        upsert=True
    )

    # 🔐 FORCE JOIN
    if not await check_channel_membership(user_id, context):
        callback_data = f"check_join_{context.args[0]}" if context.args else "check_join"

        keyboard = []

        for ch in get_support_channels():
            try:
                try:
                    chat_id = int(ch)
                except ValueError:
                    chat_id = ch if ch.startswith("@") else f"@{ch}"

                member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)

                # skip joined
                if member.status in (
                    ChatMember.MEMBER,
                    ChatMember.ADMINISTRATOR,
                    ChatMember.OWNER
                ):
                    continue

            except:
                pass

            invite_link = await get_channel_invite_link(context, ch)

            keyboard.append(
                [InlineKeyboardButton("✨ 𝙅𝙤𝙞𝙣 𝘾𝙝𝙖𝙣𝙣𝙚𝙡", url=invite_link)]
            )

        # FINAL BUTTON
        if not keyboard:
            keyboard.append(
                [InlineKeyboardButton("✅ Already Joined", callback_data=callback_data)]
            )
        else:
            keyboard.append(
                [InlineKeyboardButton("✅ Check", callback_data=callback_data)]
            )

        await update.message.reply_text(
            """╔═══ 🔐 Access Restricted ═══╗

🚫 You cannot access this link yet.

📢 Join all required channels below
to unlock your protected content.

👇 Complete the steps and click CHECK

╚═══════════════════════════╝""",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # 🔗 PROTECTED LINK FLOW
    if context.args:
        token = context.args[0]

        link_data = links_collection.find_one({"_id": token, "active": True})

        if link_data:
            web_app_url = f"{os.environ.get('RENDER_EXTERNAL_URL')}/join?token={token}"

            keyboard = [[
                InlineKeyboardButton("🔗 Join Group", web_app=WebAppInfo(url=web_app_url))
            ]]

            await update.message.reply_text(
                "🔐 This is a Protected Link\n\nClick the button below to proceed.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.message.reply_text("❌ Link expired or revoked")

        return

    # 👋 NORMAL START
    user_name = update.effective_user.first_name or "User"

    welcome_msg = f"""╔──────── ✧ ────────╗
      Welcome {user_name}
╚──────── ✧ ────────╝

🤖 I am your Link Protection Bot
Use /protect to create secure links."""

    await update.message.reply_text(welcome_msg)
    # ================= CALLBACK =================
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query

    # ❗ only ONE answer call
    await query.answer("Checking...", show_alert=False)

    # ================= NORMAL CHECK =================
    if query.data == "check_join":
        if await check_channel_membership(query.from_user.id, context):
            await query.message.edit_text(
                """╔═══ ✅ Verification Successful ═══╗

🎉 You have successfully joined all channels!

🚀 You can now continue using the bot.

╚═════════════════════════════════╝"""
            )
        else:
            await query.answer("❌ Not joined yet. Please join first.", show_alert=True)

    # ================= PROTECTED CHECK =================
    elif query.data.startswith("check_join_"):
        token = query.data.replace("check_join_", "")

        if await check_channel_membership(query.from_user.id, context):

            # 🔥 SAVE VERIFIED USER
            links_collection.update_one(
                {"_id": token},
                {"$addToSet": {"verified_users": query.from_user.id}}
            )

            link_data = links_collection.find_one({"_id": token, "active": True})

            if link_data:
                web_app_url = f"{os.environ.get('RENDER_EXTERNAL_URL')}/join?token={token}"

                keyboard = [[
                    InlineKeyboardButton(
                        "🔗 Join Group",
                        web_app=WebAppInfo(url=web_app_url)
                    )
                ]]

                await query.message.edit_text(
                    """╔═══ ✅ Access Granted ═══╗

🔓 Verification complete!

🚀 Click below to access your link.

╚═══════════════════════════╝""",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            else:
                await query.message.edit_text("❌ Link expired or revoked")

        else:
            await query.answer("❌ Not joined yet. Please join first.", show_alert=True)

    # ================= CREATE LINK =================
    elif query.data == "create_link":
        await query.message.reply_text(
            "To create a protected link, use:\n\n"
            "`/protect https://t.me/yourchannel`\n\n"
            "Replace with your actual link.",
            parse_mode=ParseMode.MARKDOWN
        )

    # ================= REVOKE =================
    elif query.data.startswith("revoke_"):
        link_id = query.data.replace("revoke_", "")
        await handle_revoke_link(update, context, link_id)
        # ================= PROTECT =================
async def protect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    # FORCE JOIN CHECK
    if not await check_channel_membership(update.effective_user.id, context):
        support_channel = os.environ.get("SUPPORT_CHANNEL", "").strip()

        if support_channel:
            invite_link = await get_channel_invite_link(context, support_channel)

            keyboard = [
                [InlineKeyboardButton("📢 Join Channel", url=invite_link)],
                [InlineKeyboardButton("✅ Check", callback_data="check_join")]
            ]

            await update.message.reply_text(
                "🔐 Join our channel first to use this bot.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        return

    # VALIDATION
    if not context.args or not context.args[0].startswith("https://t.me/"):
        await update.message.reply_text(
            "Usage:\n/protect https://t.me/yourlink"
        )
        return

    telegram_link = context.args[0]

    # 🔥 FIXED TOKEN
    token = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    short_id = token.upper()

    links_collection.insert_one({
        "_id": token,
        "short_id": short_id,
        "telegram_link": telegram_link,
        "created_by": update.effective_user.id,
        "created_at": datetime.datetime.now(),
        "active": True,
        "clicks": 0,
        "verified_users": []
    })

    bot_username = (await context.bot.get_me()).username
    protected_link = f"https://t.me/{bot_username}?start={token}"

    await update.message.reply_text(
        f"✅ Protected Link:\n{protected_link}"
    )


# ================= REVOKE =================
async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    if not context.args:
        await update.message.reply_text("Usage:\n/revoke ID")
        return

    link_id = context.args[0].upper()

    link_data = links_collection.find_one({
        "$or": [
            {"short_id": link_id},
            {"_id": link_id}
        ],
        "active": True
    })

    if not link_data:
        await update.message.reply_text("❌ Link not found")
        return

    links_collection.update_one(
        {"_id": link_data["_id"]},
        {"$set": {"active": False}}
    )

    await update.message.reply_text("✅ Link revoked")


# ================= STORE =================
async def store_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        users_collection.update_one(
            {"user_id": update.effective_user.id},
            {"$set": {"last_active": datetime.datetime.now()}},
            upsert=True
        )


# ================= HANDLERS =================
telegram_bot_app.add_handler(CommandHandler("start", start))
telegram_bot_app.add_handler(CommandHandler("protect", protect_command))
telegram_bot_app.add_handler(CommandHandler("revoke", revoke_command))
telegram_bot_app.add_handler(CallbackQueryHandler(button_callback))
telegram_bot_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, store_message))
# ================= FASTAPI =================
app = FastAPI()
templates = Jinja2Templates(directory="templates")

@app.on_event("startup")
async def on_startup():
    logger.info("🚀 Starting bot...")

    init_db()

    await telegram_bot_app.initialize()
    await telegram_bot_app.start()

    webhook_url = f"{os.environ.get('RENDER_EXTERNAL_URL')}/{os.environ.get('TELEGRAM_TOKEN')}"
    await telegram_bot_app.bot.set_webhook(url=webhook_url)

    logger.info(f"✅ Webhook set: {webhook_url}")


@app.on_event("shutdown")
async def on_shutdown():
    logger.info("🛑 Stopping bot...")
    await telegram_bot_app.stop()
    await telegram_bot_app.shutdown()
    client.close()


# ================= SECURE LINK API =================
@app.get("/getgrouplink/{token}")
async def get_group_link(token: str, user_id: int):

    data = links_collection.find_one({"_id": token})

    if not data:
        return {"error": "Invalid link"}

    # 🔥 SECURITY CHECK
    if user_id not in data.get("verified_users", []):
        return {"error": "Unauthorized"}

    return {"url": data["telegram_link"]}


# ================= TELEGRAM WEBHOOK =================
@app.post("/{token}")
async def telegram_webhook(request: Request, token: str):

    if token != os.environ.get("TELEGRAM_TOKEN"):
        raise HTTPException(status_code=403, detail="Invalid token")

    update_data = await request.json()
    update = Update.de_json(update_data, telegram_bot_app.bot)

    await telegram_bot_app.process_update(update)

    return Response(status_code=200)


# ================= JOIN PAGE =================
@app.get("/join")
async def join_page(request: Request, token: str):
    return templates.TemplateResponse(
        "join.html",
        {"request": request, "token": token}
    )


# ================= ROOT =================
@app.get("/")
async def root():
    return {
        "status": "ok",
        "bot": "running",
        "time": datetime.datetime.now().isoformat()
    }
