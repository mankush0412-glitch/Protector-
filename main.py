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

# --- Telegram Imports ---
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, ChatMember, ChatInviteLink
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Database Setup (MongoDB) ---
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

# ================= MULTI SUPPORT (NO UI CHANGE) =================
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
        if channel_data and channel_data.get("invite_link"):
            if channel_data.get("created_at") and \
               (datetime.datetime.now() - channel_data["created_at"]).days < 1:
                return channel_data["invite_link"]

        try:
            chat_id = int(channel_id)
        except ValueError:
            chat_id = channel_id if channel_id.startswith('@') else f"@{channel_id}"

        try:
            invite_link = await context.bot.create_chat_invite_link(
                chat_id=chat_id,
                creates_join_request=True,
                name="Bot Access Link",
                expire_date=None,
                member_limit=None
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
                chat = await context.bot.get_chat(chat_id)
                if chat.invite_link:
                    return chat.invite_link
                elif chat.username:
                    return f"https://t.me/{chat.username}"
            except Exception:
                pass

            if channel_id.startswith('-100'):
                return f"https://t.me/c/{channel_id[4:]}"
            elif channel_id.startswith('@'):
                return f"https://t.me/{channel_id[1:]}"
            else:
                return f"https://t.me/{channel_id}"
    except Exception as e:
        logger.error(f"❌ Error getting channel invite link: {e}")
        if channel_id.startswith('-100'):
            return f"https://t.me/c/{channel_id[4:]}"
        elif channel_id.startswith('@'):
            return f"https://t.me/{channel_id[1:]}"
        else:
            return f"https://t.me/{channel_id}"

# ================= MEMBERSHIP CHECK (MULTI) =================
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
            if chat_member.status not in (ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER):
                return False
        except Exception as e:
            logger.error(f"❌ Channel check error ({support_channel}): {e}")
            return False

    return True

# --- Telegram Bot Logic ---
telegram_bot_app = Application.builder().token(os.environ.get("TELEGRAM_TOKEN")).build()
 
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    # Save / update user
    users_collection.update_one(
        {"user_id": user_id},
        {"$set": {
            "username": update.effective_user.username,
            "first_name": update.effective_user.first_name,
            "last_active": datetime.datetime.now()
        }},
        upsert=True
    )

    # 🔐 FORCE JOIN — FOR ALL USERS (NORMAL + PROTECTED)
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

        # ✅ Agar already joined hai → skip
        if member.status in (ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER):
            continue

    except:
        # ⚠️ Agar error aaye toh button dikha de
        pass

    invite_link = await get_channel_invite_link(context, ch)

    keyboard.append(
        [InlineKeyboardButton("✨ 𝙅𝙤𝙞𝙣 𝘾𝙝𝙖𝙣𝙣𝙚𝙡", url=invite_link)]
    )

# 🔥 Agar sab joined ho gaye ho
if not keyboard:
    keyboard.append(
        [InlineKeyboardButton("✅ Already Joined", callback_data=callback_data)]
    )
else:
    keyboard.append(
        [InlineKeyboardButton("✅ Check", callback_data=callback_data)]
    )
# 🔥 Agar sab joined ho gaye ho
if not keyboard:
    keyboard.append(
        [InlineKeyboardButton("✅ Already Joined", callback_data=callback_data)]
    )
else:
    keyboard.append(
        [InlineKeyboardButton("✅ Check", callback_data=callback_data)]
    )

await update.message.reply_text(
    """✨ 𝐖𝐄𝐋𝐂𝐎𝐌𝐄 𝐓𝐎 𝐓𝐄𝐀𝐌 𝐋𝐄𝐀𝐃𝐄𝐑 ✨

🔐 𝐘𝐨𝐮𝐫 𝐀𝐜𝐜𝐞𝐬𝐬 𝐢𝐬 𝐁𝐞𝐢𝐧𝐠 𝐕𝐞𝐫𝐢𝐟𝐢𝐞𝐝...

📢 𝐏𝐥𝐞𝐚𝐬𝐞 𝐉𝐨𝐢𝐧 𝐀𝐥𝐥 𝐑𝐞𝐪𝐮𝐢𝐫𝐞𝐝 𝐂𝐡𝐚𝐧𝐧𝐞𝐥𝐬  
𝐓𝐨 𝐔𝐧𝐥𝐨𝐜𝐤 𝐘𝐨𝐮𝐫 𝐏𝐫𝐨𝐭𝐞𝐜𝐭𝐞𝐝 𝐋𝐢𝐧𝐤 🚀

👇 𝐂𝐨𝐦𝐩𝐥𝐞𝐭𝐞 𝐓𝐡𝐞 𝐒𝐭𝐞𝐩𝐬 𝐀𝐧𝐝 𝐂𝐥𝐢𝐜𝐤 𝐂𝐇𝐄𝐂𝐊""",
    reply_markup=InlineKeyboardMarkup(keyboard),
    parse_mode=ParseMode.MARKDOWN
)
return

  # 🔗 PROTECTED LINK FLOW (AFTER JOIN)
if context.args:
    token = context.args[0]

    link_data = links_collection.find_one({"_id": token, "active": True})

    if link_data:
        web_app_url = f"{os.environ.get('RENDER_EXTERNAL_URL')}/join?token={token}"

        keyboard = [[
            InlineKeyboardButton("🚀 𝙅𝙤𝙞𝙣 𝙂𝙧𝙤𝙪𝙥", web_app=WebAppInfo(url=web_app_url))
        ]]

        await update.message.reply_text(
            """💠 *ACCESS UNLOCK PANEL*

🔐 Your protected link is ready  
⚡ Click below to enter secure zone
""",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )

    else:
        await update.message.reply_text("❌ Link expired or revoked")

    return


# 🏠 NORMAL START (WELCOME UI)
user_name = update.effective_user.first_name or "User"

welcome_msg = (
    f"💎 *Welcome {user_name}* 💎\n\n"
    "🔐 Secure Link Protection Activated\n"
    "⚡ Fast • Safe • Advanced System\n\n"
    "📢 *Available Commands:*\n"
    "➤ /start\n"
    "➤ /protect\n"
    "➤ /help\n\n"
    "🚀 Ready to generate your protected link!"
)

keyboard = []

for ch in get_support_channels():
    invite_link = await get_channel_invite_link(context, ch)
    keyboard.append([
        InlineKeyboardButton("✨ 𝙎𝙪𝙥𝙥𝙤𝙧𝙩 𝘾𝙝𝙖𝙣𝙣𝙚𝙡", url=invite_link)
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
    """Handle button callbacks"""

    query = update.callback_query
    await query.answer()

    # ✅ CHECK JOIN BUTTON
    if query.data == "check_join":
        if await check_channel_membership(query.from_user.id, context):
            await query.message.edit_text(
                """✅ *Verification Successful*

🎉 You have successfully joined all channels!
🚀 You can now continue using the bot.
""",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await query.answer("❌ Not joined yet. Please join first.", show_alert=True)

    # ✅ CHECK JOIN WITH TOKEN
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
                    InlineKeyboardButton("🚀 𝙅𝙤𝙞𝙣 𝙂𝙧𝙤𝙪𝙥", web_app=WebAppInfo(url=web_app_url))
                ]]

                await query.message.edit_text(
                    """💠 *ACCESS GRANTED*

🔓 Verification complete  
🚀 Click below to open your link
""",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await query.message.edit_text("❌ Link expired or revoked")

        else:
            await query.answer("❌ Not joined yet. Please join first.", show_alert=True)

    # ✅ CREATE LINK BUTTON
    elif query.data == "create_link":
        await query.message.reply_text(
            """🛠 *Create Protected Link*

Use:
/protect https://t.me/yourchannel

Replace with your actual link.
""",
            parse_mode=ParseMode.MARKDOWN
        )

    # ✅ BROADCAST CONFIRM
    elif query.data == "confirm_broadcast":
        await handle_broadcast_confirmation(update, context)

    # ❌ CANCEL BROADCAST
    elif query.data == "cancel_broadcast":
        await query.message.edit_text("❌ Broadcast cancelled")

    # 🔥 REVOKE LINK
    elif query.data.startswith("revoke_"):
        link_id = query.data.replace("revoke_", "")
        await handle_revoke_link(update, context, link_id)

async def protect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create protected link for ANY Telegram link (group or channel)."""
    # Check channel membership
    if not await check_channel_membership(update.effective_user.id, context):
        support_channel = os.environ.get("SUPPORT_CHANNEL", "").strip()
        if support_channel:
            invite_link = await get_channel_invite_link(context, support_channel)
            keyboard = [
                [InlineKeyboardButton("📢 Join Channel", url=invite_link)],
                [InlineKeyboardButton("✅ Check", callback_data="check_join")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                "🔐 Join our channel first to use this bot.\n"
                "Then click 'Check' below.",
                reply_markup=reply_markup
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
    
    # Validate the link (basic check)
    if not telegram_link.startswith("https://t.me/"):
        await update.message.reply_text("❌ Invalid link. Must start with https://t.me/")
        return

token = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    short_id = token.upper()

    links_collection.insert_one({
        "_id": token,
        "short_id": short_id,
        "telegram_link": telegram_link,
        "link_type": "channel" if "/c/" in telegram_link or "/s/" in telegram_link or telegram_link.count('/') == 1 else "group",
        "created_by": update.effective_user.id,
        "created_by_name": update.effective_user.first_name,
        "created_at": datetime.datetime.now(),
        "active": True,
        "clicks": 0
    })

    bot_info = await context.bot.get_me()
    bot_username = bot_info.username
    protected_link = f"https://t.me/{bot_username}?start={token}"
    
    # Simple buttons
    keyboard = [
        [
            InlineKeyboardButton("📤 Share", url=f"https://t.me/share/url?url={protected_link}&text=🔐 Protected Link - Join via secure invitation"),
            InlineKeyboardButton("❌ Revoke", callback_data=f"revoke_{token}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Formatted message with markdown for easy copying
    await update.message.reply_text(
        f"✅ *Protected Link Created!*\n\n"
        f"🔑 *Link ID:* `{short_id}`\n"
        f"📊 *Status:* 🟢 Active\n"
        f"🔗 *Original Link:* `{telegram_link}`\n"
        f"📝 *Type:* {'Channel' if 'channel' in telegram_link else 'Group'}\n"
        f"⏰ *Created:* {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        f"🔐 *Your Protected Link:*\n"
        f"`{protected_link}`\n\n"
        f"📋 *Quick Actions:*\n"
        f"• Copy the link above\n"
        f"• Share with your audience\n"
        f"• Revoke anytime with `/revoke {short_id}`",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Revoke a link."""
    # Check channel membership
    if not await check_channel_membership(update.effective_user.id, context):
        support_channel = os.environ.get("SUPPORT_CHANNEL", "").strip()
        if support_channel:
            invite_link = await get_channel_invite_link(context, support_channel)
            keyboard = [
                [InlineKeyboardButton("📢 Join Channel", url=invite_link)],
                [InlineKeyboardButton("✅ Check", callback_data="check_join")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                "🔐 Join our channel first to use this bot.\n"
                "Then click 'Check' below.",
                reply_markup=reply_markup
            )
        return
    
    if not context.args:
        # Show user's active links
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
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message += "\nClick a button below to revoke."
        
        await update.message.reply_text(
            message,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Revoke by ID
    link_id = context.args[0].upper()
    
    # Find link
    query = {
        "$or": [
            {"short_id": link_id},
            {"_id": link_id}
        ],
        "created_by": update.effective_user.id,
        "active": True
    }
    
    link_data = links_collection.find_one(query)
    
    if not link_data:
        await update.message.reply_text("❌ Link not found")
        return
    
    # Revoke
    links_collection.update_one(
        {"_id": link_data['_id']},
        {
            "$set": {
                "active": False,
                "revoked_at": datetime.datetime.now()
            }
        }
    )
    
    await update.message.reply_text(
        f"✅ *Link Revoked!*\n\n"
        f"Link `{link_data.get('short_id', link_id)}` has been permanently revoked.\n\n"
        f"⚠️ All future access attempts will be blocked.",
        parse_mode=ParseMode.MARKDOWN
    )

async def handle_revoke_link(update: Update, context: ContextTypes.DEFAULT_TYPE, link_id: str):
    """Handle revoke button."""
    query = update.callback_query
    await query.answer()
    
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
    
    # Revoke
    links_collection.update_one(
        {"_id": link_id},
        {
            "$set": {
                "active": False,
                "revoked_at": datetime.datetime.now()
            }
        }
    )
    
    await query.message.edit_text(
        f"✅ *Link Revoked!*\n\n"
        f"Link `{link_data.get('short_id', link_id[:8])}` has been revoked.\n"
        f"👥 Final Clicks: {link_data.get('clicks', 0)}\n\n"
        f"⚠️ All access has been permanently blocked.",
        parse_mode=ParseMode.MARKDOWN
    )

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin broadcast."""
    admin_id = int(os.environ.get("ADMIN_ID", 0))
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
    
    # Safely get content_type with default fallback
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
    """Handle broadcast confirmation."""
    query = update.callback_query
    await query.answer()
    
    await query.message.edit_text("📤 *Broadcasting...*\n\nPlease wait, this may take a moment.", parse_mode=ParseMode.MARKDOWN)
    
    users = list(users_collection.find({}))
    total_users = len(users)
    successful = 0
    failed = 0
    
    message_to_broadcast = context.user_data.get('broadcast_message')
    
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
    """Show stats."""
    admin_id = int(os.environ.get("ADMIN_ID", 0))
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
    
    # Calculate total clicks
    total_clicks_result = links_collection.aggregate([
        {"$group": {"_id": None, "total_clicks": {"$sum": "$clicks"}}}
    ])
    total_clicks = 0
    for result in total_clicks_result:
        total_clicks = result.get('total_clicks', 0)
    
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
        f"• 🕐 Last Update: {datetime.datetime.now().strftime('%Y-%m-d %H:%M:%S')}",
        parse_mode=ParseMode.MARKDOWN
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help."""
    user_id = update.effective_user.id
    
    # Check channel membership
    if not await check_channel_membership(user_id, context):
        support_channel = os.environ.get("SUPPORT_CHANNEL", "").strip()
        if support_channel:
            invite_link = await get_channel_invite_link(context, support_channel)
            keyboard = [
                [InlineKeyboardButton("📢 Join Channel", url=invite_link)],
                [InlineKeyboardButton("✅ Check", callback_data="check_join")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                "🔐 Join our channel first to use this bot.\n"
                "Then click 'Check' below.",
                reply_markup=reply_markup
            )
        return
    
    keyboard = []
    
    support_channel = os.environ.get("SUPPORT_CHANNEL", "").strip()
    if support_channel:
        invite_link = await get_channel_invite_link(context, support_channel)
        keyboard.append([InlineKeyboardButton("🌟 Support Channel", url=invite_link)])
    
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
    
    # ================= LECTURE SYSTEM =================

# ➕ ADD (ADMIN | REPLY REQUIRED)
async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = int(os.environ.get("ADMIN_ID", 0))

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


# 📚 LECTURE LIST (PUBLIC)
async def lecture_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lectures = list(lectures_collection.find().sort("created_at", 1))

    if not lectures:
        await update.message.reply_text("📭 Abhi koi lecture add nahi hai.")
        return

    # ❌ Markdown hata diya taaki "_" safe rahe
    message = "📚 Lecture List\n\n"

    for i, lec in enumerate(lectures, start=1):
        # ❌ Stars (*) hata diye
        message += f"{i}. {lec['content']}\n\n"

    # ❌ parse_mode=MARKDOWN hata diya
    await update.message.reply_text(
        message[:4096],
        disable_web_page_preview=True
    )


# ❌ DELETE (ADMIN ONLY | NUMBER BASED)
async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = int(os.environ.get("ADMIN_ID", 0))

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
    """Store user activity."""
    if update.message and update.message.chat.type == "private":
        users_collection.update_one(
            {"user_id": update.effective_user.id},
            {"$set": {"last_active": update.message.date}},
            upsert=True
        )

# Register handlers
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

# Add callback handler
from telegram.ext import CallbackQueryHandler
telegram_bot_app.add_handler(CallbackQueryHandler(button_callback))

# --- FastAPI Setup ---
app = FastAPI()
templates = Jinja2Templates(directory="templates")

@app.on_event("startup")
async def on_startup():
    """Start bot."""
    logger.info("Starting bot...")
    
    required_vars = ["TELEGRAM_TOKEN", "RENDER_EXTERNAL_URL"]
    for var in required_vars:
        if not os.environ.get(var):
            logger.critical(f"Missing {var}")
            raise Exception(f"Missing {var}")
    
    init_db()
    
    await telegram_bot_app.initialize()
    await telegram_bot_app.start()
    
    webhook_url = f"{os.environ.get('RENDER_EXTERNAL_URL')}/{os.environ.get('TELEGRAM_TOKEN')}"
    await telegram_bot_app.bot.set_webhook(url=webhook_url)
    logger.info(f"Webhook: {webhook_url}")
    
    bot_info = await telegram_bot_app.bot.get_me()
    logger.info(f"Bot: @{bot_info.username}")
    
    # Test channel link generation
    support_channel = os.environ.get("SUPPORT_CHANNEL", "").strip()
    if support_channel:
        try:
            invite_link = await get_channel_invite_link(telegram_bot_app, support_channel)
            logger.info(f"Support channel invite link: {invite_link}")
        except Exception as e:
            logger.error(f"Failed to generate channel link: {e}")

@app.on_event("shutdown")
async def on_shutdown():
    """Stop bot."""
    logger.info("Stopping bot...")
    await telegram_bot_app.stop()
    await telegram_bot_app.shutdown()
    client.close()
    logger.info("Bot stopped")
@app.get("/getgrouplink/{token}")
async def get_group_link(token: str, user_id: int):

    data = links_collection.find_one({"_id": token})

    if not data:
        return {"error": "Invalid link"}

    # 🔥 SECURITY CHECK
    if user_id not in data.get("verified_users", []):
        return {"error": "Unauthorized"}

    return {"url": data["telegram_link"]}

@app.post("/{token}")
async def telegram_webhook(request: Request, token: str):
    """Telegram webhook."""
    if token != os.environ.get("TELEGRAM_TOKEN"):
        raise HTTPException(status_code=403, detail="Invalid token")
    
    update_data = await request.json()
    update = Update.de_json(update_data, telegram_bot_app.bot)
    await telegram_bot_app.process_update(update)
    
    return Response(status_code=200)

@app.get("/join")
async def join_page(request: Request, token: str):
    """Web app page."""
    return templates.TemplateResponse("join.html", {"request": request, "token": token})

@app.get("/")
async def root():
    """Health check."""
    return {
        "status": "ok",
        "service": "LinkShield Pro",
        "version": "2.0.0",
        "time": datetime.datetime.now().isoformat()
    }
