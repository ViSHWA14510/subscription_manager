import logging
from datetime import datetime, timedelta, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMember
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ChatMemberHandler, ContextTypes, MessageHandler, filters
)
from database import Database
from config import Config

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

db = Database()


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def is_owner(user_id: int) -> bool:
    return user_id == Config.OWNER_ID


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_expiry(raw) -> datetime:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(raw)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt_remaining(expiry: datetime) -> str:
    delta = expiry - now_utc()
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "Expired"
    days = delta.days
    hours, rem = divmod(total_seconds % 86400, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    return f"{hours}h {minutes}m"


def status_emoji(expiry: datetime) -> str:
    delta = (expiry - now_utc()).days
    if delta > 7:
        return "🟢"
    elif delta > 0:
        return "🟡"
    return "🔴"


def sub_info_text(username: str, user_id: int, channel_name: str,
                  join_date: datetime, expiry: datetime) -> str:
    """Standard subscription info block used everywhere."""
    return (
        "━━━━ SUBSCRIPTION INFO ━━━━\n\n"
        f"👤 Username:- @{username or 'N/A'}\n"
        f"🆔 User Id:- <code>{user_id}</code>\n"
        f"📢 Premium for Channel:- <b>{channel_name}</b>\n"
        f"📅 Join Date:- <b>{join_date.strftime('%d %b %Y')}</b>\n"
        f"📆 Expire Date:- <b>{expiry.strftime('%d %b %Y')}</b>\n"
        f"⏳ Expires In:- <b>{fmt_remaining(expiry)}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━"
    )


async def send_sub_info(bot, chat_id: int, username: str, user_id: int,
                        channel_name: str, join_date: datetime, expiry: datetime,
                        photo_file_id: str = None, extra_caption: str = ""):
    """Send subscription info — with photo if available, plain text otherwise."""
    text = sub_info_text(username, user_id, channel_name, join_date, expiry)
    if extra_caption:
        text = extra_caption + "\n\n" + text
    if photo_file_id:
        await bot.send_photo(
            chat_id=chat_id,
            photo=photo_file_id,
            caption=text,
            parse_mode="HTML"
        )
    else:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")


async def get_user_info(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    try:
        return await context.bot.get_chat(user_id)
    except Exception:
        return None


def owner_panel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 All Users", callback_data="ap_users"),
         InlineKeyboardButton("📢 Channels", callback_data="ap_channels")],
        [InlineKeyboardButton("➕ Add / Extend Sub", callback_data="ap_add_start")],
        [InlineKeyboardButton("📣 Broadcast", callback_data="ap_broadcast_prompt")],
    ])


# ─────────────────────────────────────────────
#  /start
# ─────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_owner(user.id):
        await update.message.reply_text(
            f"👋 Welcome back, <b>{user.first_name}</b>!\n\n"
            "Use /admin to open the Admin Panel.",
            parse_mode="HTML"
        )
    else:
        keyboard = [[InlineKeyboardButton("📅 My Subscriptions", callback_data="my_subscription")]]
        await update.message.reply_text(
            f"👋 Hello, <b>{user.first_name}</b>!\n\n"
            "Use the button below to check your subscription status.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )


# ─────────────────────────────────────────────
#  /admin — Admin Panel
# ─────────────────────────────────────────────

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Access denied.")
        return
    subs = db.get_all_subscriptions()
    channels = db.get_managed_channels()
    active = [s for s in subs if parse_expiry(s["expiry_date"]) > now_utc()]
    text = (
        "🛠 <b>Admin Panel</b>\n\n"
        f"👥 Total active subscribers: <b>{len(active)}</b>\n"
        f"📢 Managed channels: <b>{len(channels)}</b>\n\n"
        "Choose an action:"
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=owner_panel_keyboard())


# ─────────────────────────────────────────────
#  CHAT MEMBER HANDLER (auto-detect joins)
# ─────────────────────────────────────────────

async def chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member
    if not result:
        return

    old_status = result.old_chat_member.status
    new_status = result.new_chat_member.status
    user = result.new_chat_member.user
    chat = result.chat

    if old_status in [ChatMember.LEFT, ChatMember.BANNED] and new_status == ChatMember.MEMBER:
        channel_id = str(chat.id)
        if not db.is_managed_channel(channel_id):
            return

        existing = db.get_subscription(user.id, channel_id)
        plan = db.get_channel_plan(channel_id)
        expiry = now_utc() + timedelta(days=30 * plan)
        join_date = now_utc()

        if existing:
            db.update_subscription_expiry(user.id, channel_id, expiry)
            action = "Renewed"
        else:
            db.add_subscription(user.id, channel_id, expiry, user.username or user.first_name)
            action = "Started"

        logger.info(f"Subscription {action} for user {user.id} in channel {channel_id}")

        # Notify user
        try:
            await send_sub_info(
                context.bot, user.id,
                username=user.username or user.first_name,
                user_id=user.id,
                channel_name=chat.title,
                join_date=join_date,
                expiry=expiry,
                extra_caption=f"✅ <b>Subscription {action}!</b>"
            )
        except Exception:
            pass

        # Notify owner
        await send_sub_info(
            context.bot, Config.OWNER_ID,
            username=user.username or user.first_name,
            user_id=user.id,
            channel_name=chat.title,
            join_date=join_date,
            expiry=expiry,
            extra_caption=f"🆕 <b>New Subscription ({action})</b>"
        )

    elif old_status == ChatMember.MEMBER and new_status in [ChatMember.LEFT, ChatMember.BANNED]:
        channel_id = str(chat.id)
        if db.is_managed_channel(channel_id):
            db.deactivate_subscription(user.id, channel_id)
            logger.info(f"User {user.id} left {channel_id}, subscription deactivated")


# ─────────────────────────────────────────────
#  CALLBACK HANDLER
# ─────────────────────────────────────────────

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user

    # ── User: My Subscriptions ─────────────────────────
    if data == "my_subscription":
        subs = db.get_user_subscriptions(user.id)
        if not subs:
            await query.edit_message_text(
                "❌ You have no active subscriptions.\n\nJoin a premium channel to get started!",
                parse_mode="HTML"
            )
            return
        text = "📋 <b>Your Subscriptions</b>\n\n"
        for sub in subs:
            expiry = parse_expiry(sub["expiry_date"])
            started = parse_expiry(sub.get("created_at", sub["expiry_date"]))
            emoji = status_emoji(expiry)
            text += (
                f"{emoji} <b>{sub.get('channel_name', 'Unknown')}</b>\n"
                f"   📅 Expires: {expiry.strftime('%d %b %Y %H:%M')} UTC\n"
                f"   ⏳ Remaining: {fmt_remaining(expiry)}\n"
                f"   🗓 Started: {started.strftime('%d %b %Y')}\n\n"
            )
        await query.edit_message_text(text, parse_mode="HTML")

    # ── Admin: Back ────────────────────────────────────
    elif data == "ap_back" and is_owner(user.id):
        subs = db.get_all_subscriptions()
        channels = db.get_managed_channels()
        active = [s for s in subs if parse_expiry(s["expiry_date"]) > now_utc()]
        text = (
            "🛠 <b>Admin Panel</b>\n\n"
            f"👥 Total active subscribers: <b>{len(active)}</b>\n"
            f"📢 Managed channels: <b>{len(channels)}</b>\n\n"
            "Choose an action:"
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=owner_panel_keyboard())

    # ── Admin: All Users ───────────────────────────────
    elif data == "ap_users" and is_owner(user.id):
        await _show_users_list(query)

    elif data.startswith("ap_users_page_") and is_owner(user.id):
        page = int(data.split("_")[-1])
        await _show_users_list(query, page=page)

    # ── Admin: User detail ─────────────────────────────
    elif data.startswith("ap_user_") and is_owner(user.id):
        uid = int(data.split("_")[2])
        await _show_user_detail(query, uid)

    # ── Admin: Channels ────────────────────────────────
    elif data == "ap_channels" and is_owner(user.id):
        await _show_channels(query)

    # ── Admin: Add/Extend — pick channel ──────────────
    elif data == "ap_add_start" and is_owner(user.id):
        context.user_data.clear()
        channels = db.get_managed_channels()
        if not channels:
            await query.edit_message_text(
                "⚠️ No managed channels yet. Add one with /addchannel first.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="ap_back")]])
            )
            return
        keyboard = [
            [InlineKeyboardButton(f"📢 {ch['name']}", callback_data=f"ap_pick_ch_{ch['channel_id']}")]
            for ch in channels
        ]
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="ap_back")])
        await query.edit_message_text(
            "➕ <b>Add / Extend Subscription</b>\n\nStep 1 — Pick a channel:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("ap_pick_ch_") and is_owner(user.id):
        channel_id = data[len("ap_pick_ch_"):]
        context.user_data["add_channel_id"] = channel_id
        ch = db.get_channel_info(channel_id)
        context.user_data["add_channel_name"] = ch["name"] if ch else channel_id
        await query.edit_message_text(
            f"📢 Channel: <b>{context.user_data['add_channel_name']}</b>\n\n"
            "Step 2 — Send me the <b>Telegram User ID</b> of the user:\n"
            "<i>(You can get it via @userinfobot)</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="ap_back")]])
        )
        context.user_data["awaiting"] = "user_id"

    # ── Admin: Extend from user detail ────────────────
    elif data.startswith("ap_extend_") and is_owner(user.id):
        parts = data.split("_")
        uid = int(parts[2])
        channel_id = "_".join(parts[3:])
        context.user_data["add_channel_id"] = channel_id
        context.user_data["add_user_id"] = uid
        ch = db.get_channel_info(channel_id)
        context.user_data["add_channel_name"] = ch["name"] if ch else channel_id
        await query.edit_message_text(
            f"🔧 <b>Extend Subscription</b>\n\n"
            f"👤 User ID: <code>{uid}</code>\n"
            f"📢 Channel: <b>{context.user_data['add_channel_name']}</b>\n\n"
            "How many <b>months</b> to add?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"ap_user_{uid}")]])
        )
        context.user_data["awaiting"] = "extend_months"

    # ── Admin: Remove sub from detail ─────────────────
    elif data.startswith("ap_removesub_") and is_owner(user.id):
        parts = data.split("_")
        uid = int(parts[2])
        channel_id = "_".join(parts[3:])
        db.remove_subscription(uid, channel_id)
        try:
            await context.bot.ban_chat_member(channel_id, uid)
            await context.bot.unban_chat_member(channel_id, uid)
        except Exception as e:
            logger.warning(f"Could not kick {uid}: {e}")
        try:
            await context.bot.send_message(
                uid,
                "❌ <b>Subscription Removed</b>\n\nYour subscription has been removed by the admin.",
                parse_mode="HTML"
            )
        except Exception:
            pass
        await query.edit_message_text(
            f"✅ Subscription removed for user <code>{uid}</code>.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="ap_users")]])
        )

    # ── Admin: Broadcast prompt ────────────────────────
    elif data == "ap_broadcast_prompt" and is_owner(user.id):
        await query.edit_message_text(
            "📣 <b>Broadcast</b>\n\nUse command:\n<code>/broadcast Your message here</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="ap_back")]])
        )

    # ── Admin: Skip screenshot ─────────────────────────
    elif data == "ap_skip_photo" and is_owner(user.id):
        await _finish_add_sub(update, context, photo_file_id=None)


# ─────────────────────────────────────────────
#  ADMIN PANEL HELPERS
# ─────────────────────────────────────────────

async def _show_users_list(query, page: int = 0):
    PAGE_SIZE = 8
    subs = db.get_all_subscriptions()
    seen = {}
    for s in subs:
        uid = s["user_id"]
        expiry = parse_expiry(s["expiry_date"])
        if uid not in seen or expiry < parse_expiry(seen[uid]["expiry_date"]):
            seen[uid] = s
    users = sorted(seen.values(), key=lambda s: parse_expiry(s["expiry_date"]))
    total = len(users)
    start = page * PAGE_SIZE
    page_users = users[start:start + PAGE_SIZE]

    if not page_users:
        await query.edit_message_text(
            "📭 No subscribers found.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="ap_back")]])
        )
        return

    text = f"👥 <b>Subscribers</b> ({total} total) — Page {page + 1}\n\n"
    keyboard = []
    for sub in page_users:
        expiry = parse_expiry(sub["expiry_date"])
        emoji = status_emoji(expiry)
        label = f"{emoji} {sub.get('username', str(sub['user_id']))} — {fmt_remaining(expiry)}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"ap_user_{sub['user_id']}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"ap_users_page_{page - 1}"))
    if start + PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"ap_users_page_{page + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="ap_back")])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def _show_user_detail(query, uid: int):
    all_subs = db.get_all_subscriptions_for_user(uid)
    if not all_subs:
        await query.edit_message_text(
            f"❌ No subscriptions found for user <code>{uid}</code>.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="ap_users")]])
        )
        return

    text = f"👤 <b>User Detail</b> — <code>{uid}</code>\n\n"
    action_buttons = []
    for sub in all_subs:
        expiry = parse_expiry(sub["expiry_date"])
        started = parse_expiry(sub.get("created_at", sub["expiry_date"]))
        is_active = sub.get("is_active", True)
        emoji = status_emoji(expiry) if is_active else "⚫"
        channel_name = sub.get("channel_name", sub["channel_id"])
        channel_id = sub["channel_id"]
        text += (
            f"{emoji} <b>{channel_name}</b>\n"
            f"   🗓 Join Date: {started.strftime('%d %b %Y')}\n"
            f"   📆 Expire Date: {expiry.strftime('%d %b %Y %H:%M')} UTC\n"
            f"   ⏳ Expires In: {fmt_remaining(expiry)}\n"
            f"   Status: {'✅ Active' if is_active else '❌ Inactive'}\n\n"
        )
        if is_active:
            action_buttons.append([
                InlineKeyboardButton(f"➕ Extend ({channel_name[:12]})", callback_data=f"ap_extend_{uid}_{channel_id}"),
                InlineKeyboardButton("🗑 Remove", callback_data=f"ap_removesub_{uid}_{channel_id}"),
            ])

    action_buttons.append([InlineKeyboardButton("🔙 Back to Users", callback_data="ap_users")])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(action_buttons))


async def _show_channels(query):
    channels = db.get_managed_channels()
    if not channels:
        text = "📭 No managed channels yet.\n\nUse /addchannel &lt;id&gt; &lt;months&gt; &lt;name&gt;"
    else:
        text = "📢 <b>Managed Channels</b>\n\n"
        for ch in channels:
            subs = db.get_all_subscriptions(ch["channel_id"])
            active = [s for s in subs if parse_expiry(s["expiry_date"]) > now_utc()]
            text += (
                f"• <b>{ch['name']}</b>\n"
                f"  ID: <code>{ch['channel_id']}</code>\n"
                f"  Plan: {ch['plan_months']} month(s) | Active subs: {len(active)}\n\n"
            )
    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="ap_back")]])
    )


async def _finish_add_sub(update_or_query, context: ContextTypes.DEFAULT_TYPE, photo_file_id: str = None):
    """Final step: save subscription, notify user & owner with sub info card."""
    data = context.user_data
    uid = data["add_user_id"]
    channel_id = data["add_channel_id"]
    channel_name = data["add_channel_name"]
    months = data["add_months"]
    action = data.get("add_action", "new")  # "new" or "extend"

    user_info = await get_user_info(context, uid)
    username = (user_info.username or user_info.first_name or str(uid)) if user_info else str(uid)

    if action == "extend":
        existing = db.get_subscription(uid, channel_id)
        if existing:
            current_expiry = parse_expiry(existing["expiry_date"])
            base = current_expiry if current_expiry > now_utc() else now_utc()
        else:
            base = now_utc()
        new_expiry = base + timedelta(days=30 * months)
        db.update_subscription_expiry(uid, channel_id, new_expiry)
        join_date = parse_expiry(existing["created_at"]) if existing and existing.get("created_at") else now_utc()
        caption_prefix = "🔄 <b>Subscription Extended!</b>"
    else:
        new_expiry = now_utc() + timedelta(days=30 * months)
        join_date = now_utc()
        db.add_subscription(uid, channel_id, new_expiry, username)
        caption_prefix = "🎉 <b>Subscription Added!</b>"

    context.user_data.clear()

    # Notify user
    try:
        await send_sub_info(
            context.bot, uid,
            username=username,
            user_id=uid,
            channel_name=channel_name,
            join_date=join_date,
            expiry=new_expiry,
            photo_file_id=photo_file_id,
            extra_caption=caption_prefix
        )
    except Exception as e:
        logger.warning(f"Could not notify user {uid}: {e}")

    # Notify owner
    await send_sub_info(
        context.bot, Config.OWNER_ID,
        username=username,
        user_id=uid,
        channel_name=channel_name,
        join_date=join_date,
        expiry=new_expiry,
        photo_file_id=photo_file_id,
        extra_caption=f"✅ <b>Done — {caption_prefix}</b>"
    )

    confirm = (
        f"✅ {'Extended' if action == 'extend' else 'Added'} subscription for "
        f"<code>{uid}</code> (@{username})\n"
        f"📢 {channel_name} — expires <b>{new_expiry.strftime('%d %b %Y')}</b>"
    )
    # send confirmation depending on context type
    if hasattr(update_or_query, 'callback_query'):
        await update_or_query.effective_message.reply_text(confirm, parse_mode="HTML")
    else:
        await update_or_query.message.reply_text(confirm, parse_mode="HTML")


# ─────────────────────────────────────────────
#  MESSAGE HANDLER — multi-step add/extend flow
# ─────────────────────────────────────────────

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return

    awaiting = context.user_data.get("awaiting")
    if not awaiting:
        return

    text = update.message.text.strip()

    # Step 1: User ID
    if awaiting == "user_id":
        if not text.isdigit():
            await update.message.reply_text("❗ Please send a valid numeric Telegram User ID.")
            return
        uid = int(text)
        context.user_data["add_user_id"] = uid

        existing = db.get_subscription(uid, context.user_data["add_channel_id"])
        user_info = await get_user_info(context, uid)
        name = (user_info.first_name or str(uid)) if user_info else str(uid)

        if existing:
            expiry = parse_expiry(existing["expiry_date"])
            await update.message.reply_text(
                f"👤 User: <b>{name}</b> (<code>{uid}</code>)\n"
                f"📢 Channel: <b>{context.user_data['add_channel_name']}</b>\n\n"
                f"⚠️ Already subscribed — expires <b>{expiry.strftime('%d %b %Y')}</b> ({fmt_remaining(expiry)} left)\n\n"
                "How many <b>months</b> to extend?",
                parse_mode="HTML"
            )
            context.user_data["add_action"] = "extend"
        else:
            await update.message.reply_text(
                f"👤 User: <b>{name}</b> (<code>{uid}</code>)\n"
                f"📢 Channel: <b>{context.user_data['add_channel_name']}</b>\n\n"
                "How many <b>months</b> for this subscription?",
                parse_mode="HTML"
            )
            context.user_data["add_action"] = "new"
        context.user_data["awaiting"] = "months"

    # Step 2: Months
    elif awaiting == "months" or awaiting == "extend_months":
        if not text.isdigit() or int(text) < 1:
            await update.message.reply_text("❗ Please send a valid number of months (e.g. 1, 3, 6).")
            return
        context.user_data["add_months"] = int(text)
        context.user_data["awaiting"] = "photo"

        await update.message.reply_text(
            "📸 <b>Payment Screenshot</b>\n\n"
            "Send the payment screenshot now, or tap <b>Skip</b> to proceed without one.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭ Skip Screenshot", callback_data="ap_skip_photo")]
            ])
        )

    # Step 3: handled by photo handler below


async def handle_photo_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receives payment screenshot from owner during the add/extend flow."""
    if not is_owner(update.effective_user.id):
        return
    if context.user_data.get("awaiting") != "photo":
        return

    # Use the highest-resolution version of the photo
    photo = update.message.photo[-1]
    context.user_data["awaiting"] = None
    await _finish_add_sub(update, context, photo_file_id=photo.file_id)


# ─────────────────────────────────────────────
#  OWNER COMMANDS
# ─────────────────────────────────────────────

async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /addchannel <channel_id> <plan_months> <channel_name>"""
    if not is_owner(update.effective_user.id):
        return
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "❗ Usage: /addchannel <channel_id> <plan_months> <channel_name>\n"
            "Example: /addchannel -1001234567890 1 My Premium Channel"
        )
        return
    channel_id = args[0]
    try:
        plan_months = int(args[1])
    except ValueError:
        await update.message.reply_text("❗ Plan months must be a number.")
        return
    channel_name = " ".join(args[2:])
    try:
        bot_member = await context.bot.get_chat_member(channel_id, context.bot.id)
        if bot_member.status not in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
            await update.message.reply_text("❗ Bot is not an admin in that channel.")
            return
    except Exception as e:
        await update.message.reply_text(f"❗ Could not verify channel: {e}")
        return
    db.add_managed_channel(channel_id, channel_name, plan_months)
    await update.message.reply_text(
        f"✅ Channel <b>{channel_name}</b> added!\n📅 Default plan: <b>{plan_months} month(s)</b>",
        parse_mode="HTML"
    )


async def remove_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("❗ Usage: /removechannel <channel_id>")
        return
    db.remove_managed_channel(context.args[0])
    await update.message.reply_text(f"✅ Channel <code>{context.args[0]}</code> removed.", parse_mode="HTML")


async def add_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /addsub <user_id> <channel_id> <months>  — quick command, no screenshot"""
    if not is_owner(update.effective_user.id):
        return
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("❗ Usage: /addsub <user_id> <channel_id> <months>")
        return
    try:
        user_id, channel_id, months = int(args[0]), args[1], int(args[2])
    except ValueError:
        await update.message.reply_text("❗ Invalid arguments.")
        return

    expiry = now_utc() + timedelta(days=30 * months)
    join_date = now_utc()
    user_info = await get_user_info(context, user_id)
    username = (user_info.username or user_info.first_name) if user_info else str(user_id)
    db.add_subscription(user_id, channel_id, expiry, username)

    ch = db.get_channel_info(channel_id)
    channel_name = ch["name"] if ch else channel_id

    try:
        await send_sub_info(
            context.bot, user_id,
            username=username, user_id=user_id,
            channel_name=channel_name,
            join_date=join_date, expiry=expiry,
            extra_caption="🎉 <b>Subscription Added!</b>"
        )
    except Exception:
        pass

    await send_sub_info(
        context.bot, Config.OWNER_ID,
        username=username, user_id=user_id,
        channel_name=channel_name,
        join_date=join_date, expiry=expiry,
        extra_caption="✅ <b>Subscription Added via Command</b>"
    )


async def remove_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("❗ Usage: /removesub <user_id> <channel_id>")
        return
    user_id, channel_id = int(args[0]), args[1]
    db.remove_subscription(user_id, channel_id)
    try:
        await context.bot.ban_chat_member(channel_id, user_id)
        await context.bot.unban_chat_member(channel_id, user_id)
    except Exception as e:
        logger.warning(f"Could not kick {user_id}: {e}")
    try:
        await context.bot.send_message(
            user_id,
            "❌ <b>Subscription Removed</b>\n\nYour subscription has been removed by the admin.",
            parse_mode="HTML"
        )
    except Exception:
        pass
    await update.message.reply_text(f"✅ Subscription removed for <code>{user_id}</code>.", parse_mode="HTML")


async def list_subs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    channel_id = context.args[0] if context.args else None
    subs = db.get_all_subscriptions(channel_id)
    if not subs:
        await update.message.reply_text("📭 No subscriptions found.")
        return
    text = "📋 <b>Subscriptions</b>\n\n"
    for sub in subs:
        expiry = parse_expiry(sub["expiry_date"])
        emoji = status_emoji(expiry)
        text += (
            f"{emoji} <b>{sub.get('username', 'Unknown')}</b> (<code>{sub['user_id']}</code>)\n"
            f"   {sub.get('channel_name', sub['channel_id'])} — {fmt_remaining(expiry)}\n\n"
        )
    if len(text) > 4000:
        text = text[:4000] + "\n... (truncated)\n\nTip: use /admin for full panel."
    await update.message.reply_text(text, parse_mode="HTML")


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("❗ Usage: /broadcast <message>")
        return
    msg = " ".join(context.args)
    subs = db.get_all_subscriptions()
    user_ids = list(set(s["user_id"] for s in subs))
    sent, failed = 0, 0
    for uid in user_ids:
        try:
            await context.bot.send_message(uid, f"📣 <b>Announcement</b>\n\n{msg}", parse_mode="HTML")
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"✅ Broadcast done!\n✉️ Sent: {sent} | ❌ Failed: {failed}")


# ─────────────────────────────────────────────
#  SCHEDULER
# ─────────────────────────────────────────────

async def check_expiring_subscriptions(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Running subscription expiry check...")

    for sub in db.get_expiring_subscriptions(days=3):
        expiry = parse_expiry(sub["expiry_date"])
        channel_name = sub.get("channel_name", sub["channel_id"])
        join_date = parse_expiry(sub.get("created_at", sub["expiry_date"]))
        try:
            await send_sub_info(
                context.bot, sub["user_id"],
                username=sub.get("username", ""),
                user_id=sub["user_id"],
                channel_name=channel_name,
                join_date=join_date,
                expiry=expiry,
                extra_caption="⚠️ <b>Subscription Expiring Soon!</b>\nPlease contact the admin to renew."
            )
            db.mark_notified(sub["user_id"], sub["channel_id"], "3d")
        except Exception as e:
            logger.warning(f"Could not notify {sub['user_id']}: {e}")
        try:
            await send_sub_info(
                context.bot, Config.OWNER_ID,
                username=sub.get("username", ""),
                user_id=sub["user_id"],
                channel_name=channel_name,
                join_date=join_date,
                expiry=expiry,
                extra_caption="⚠️ <b>Expiring Soon — Heads Up</b>"
            )
        except Exception:
            pass

    for sub in db.get_expired_subscriptions():
        expiry = parse_expiry(sub["expiry_date"])
        channel_name = sub.get("channel_name", sub["channel_id"])
        join_date = parse_expiry(sub.get("created_at", sub["expiry_date"]))
        try:
            await send_sub_info(
                context.bot, sub["user_id"],
                username=sub.get("username", ""),
                user_id=sub["user_id"],
                channel_name=channel_name,
                join_date=join_date,
                expiry=expiry,
                extra_caption="❌ <b>Subscription Expired!</b>\nContact the admin to renew."
            )
        except Exception:
            pass
        try:
            await context.bot.ban_chat_member(sub["channel_id"], sub["user_id"])
            await context.bot.unban_chat_member(sub["channel_id"], sub["user_id"])
        except Exception as e:
            logger.warning(f"Could not kick {sub['user_id']}: {e}")
        try:
            await send_sub_info(
                context.bot, Config.OWNER_ID,
                username=sub.get("username", ""),
                user_id=sub["user_id"],
                channel_name=channel_name,
                join_date=join_date,
                expiry=expiry,
                extra_caption="❌ <b>Expired & Removed from Channel</b>"
            )
        except Exception:
            pass
        db.mark_expired_notified(sub["user_id"], sub["channel_id"])


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def main():
    db.init_db()
    app = Application.builder().token(Config.BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("addchannel", add_channel))
    app.add_handler(CommandHandler("removechannel", remove_channel))
    app.add_handler(CommandHandler("addsub", add_subscription))
    app.add_handler(CommandHandler("removesub", remove_subscription))
    app.add_handler(CommandHandler("listsubs", list_subs))
    app.add_handler(CommandHandler("broadcast", broadcast))

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(ChatMemberHandler(chat_member_update, ChatMemberHandler.CHAT_MEMBER))

    # Photo handler for payment screenshots (owner only, during flow)
    app.add_handler(MessageHandler(
        filters.PHOTO & filters.User(Config.OWNER_ID),
        handle_photo_input
    ))

    # Text handler for multi-step flow (owner only)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.User(Config.OWNER_ID),
        handle_text_input
    ))

    app.job_queue.run_repeating(check_expiring_subscriptions, interval=43200, first=30)

    logger.info("Bot started...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
