import logging
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

def to_ist(dt: datetime) -> datetime:
    """Convert any UTC-aware datetime to IST."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)

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
#  PAYMENT PLANS  ← EDIT PRICES HERE
#  Format: (label, months, price_in_rupees)
# ─────────────────────────────────────────────
PLANS = [
    ("1 Month",  1,   499),
    ("3 Months", 3,  1299),
    ("6 Months", 6,  2299),
    ("1 Year",  12,  3999),
]

# ─────────────────────────────────────────────
#  UPI CONFIG  ← SET YOUR UPI ID & QR HERE
# ─────────────────────────────────────────────
UPI_ID   = Config.UPI_ID        # set in .env: UPI_ID=yourname@upi
QR_FILE_ID = Config.UPI_QR_FILE_ID  # set in .env after sending QR to bot once


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def is_owner(user_id: int) -> bool:
    return user_id == Config.OWNER_ID


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(raw) -> datetime:
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
                  join_date: datetime, expiry: datetime,
                  first_name: str = "") -> str:
    display_name = first_name or username or "N/A"
    user_link = f'<a href="tg://user?id={user_id}">{display_name}</a>'
    username_line = f"@{username}" if username else "N/A"
    return (
        "━━━━ SUBSCRIPTION INFO ━━━━\n\n"
        f"👤 Name:- {user_link}\n"
        f"🔖 Username:- {username_line}\n"
        f"🆔 User Id:- <code>{user_id}</code>\n"
        f"📢 Channel:- <b>{channel_name}</b>\n"
        f"📅 Join Date:- <b>{to_ist(join_date).strftime('%d %b %Y %H:%M')} IST</b>\n"
        f"📆 Expire Date:- <b>{to_ist(expiry).strftime('%d %b %Y %H:%M')} IST</b>\n"
        f"⏳ Expires In:- <b>{fmt_remaining(expiry)}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━"
    )


async def send_sub_info(bot, chat_id: int, username: str, user_id: int,
                        channel_name: str, join_date: datetime, expiry: datetime,
                        photo_file_id: str = None, extra_caption: str = "",
                        reply_markup=None, first_name: str = ""):
    text = sub_info_text(username, user_id, channel_name, join_date, expiry, first_name=first_name)
    if extra_caption:
        text = extra_caption + "\n\n" + text
    kwargs = {"parse_mode": "HTML"}
    if reply_markup:
        kwargs["reply_markup"] = reply_markup
    if photo_file_id:
        await bot.send_photo(chat_id=chat_id, photo=photo_file_id, caption=text, **kwargs)
    else:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)


async def get_user_info(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    try:
        return await context.bot.get_chat(user_id)
    except Exception:
        return None


def owner_panel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 All Users", callback_data="ap_users"),
         InlineKeyboardButton("📢 Channels", callback_data="ap_channels")],
        [InlineKeyboardButton("⏳ Pending Approvals", callback_data="ap_pending"),
         InlineKeyboardButton("💳 Payment Requests", callback_data="ap_payments")],
        [InlineKeyboardButton("➕ Add / Extend Sub", callback_data="ap_add_start")],
        [InlineKeyboardButton("📣 Broadcast", callback_data="ap_broadcast_prompt")],
    ])


def month_picker_keyboard(user_id: int, channel_id: str, prefix: str) -> InlineKeyboardMarkup:
    """Inline month-picker buttons for owner approval flow."""
    months = [1, 2, 3, 6, 12]
    row = [
        InlineKeyboardButton(f"{m}M", callback_data=f"{prefix}{user_id}_{channel_id}_{m}")
        for m in months
    ]
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("❌ Cancel", callback_data="ap_back")]])


# ─────────────────────────────────────────────
#  INVITE LINK — create & schedule revoke
# ─────────────────────────────────────────────

async def create_invite_and_schedule_revoke(
    bot, job_queue, channel_id: str, user_id: int
) -> str:
    """
    Creates a one-time invite link valid for 1 hour.
    Schedules a job to revoke it after 1 hour.
    Returns the invite link URL.
    """
    expire_at = now_utc() + timedelta(hours=1)
    link_obj = await bot.create_chat_invite_link(
        chat_id=channel_id,
        expire_date=expire_at,
        member_limit=1,          # one-time use
        name=f"pay_{user_id}"
    )
    invite_url = link_obj.invite_link

    # Schedule revocation after 1 hour
    job_queue.run_once(
        _revoke_invite_link,
        when=3600,
        data={"channel_id": channel_id, "invite_link": invite_url},
        name=f"revoke_{user_id}_{channel_id}"
    )
    return invite_url


async def _revoke_invite_link(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    try:
        await context.bot.revoke_chat_invite_link(
            chat_id=data["channel_id"],
            invite_link=data["invite_link"]
        )
        logger.info(f"Revoked invite link for channel {data['channel_id']}")
    except Exception as e:
        logger.warning(f"Could not revoke invite link: {e}")


# ─────────────────────────────────────────────
#  /start
# ─────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_owner(user.id):
        await update.message.reply_text(
            "╔══════════════════════╗\n"
            "   🌟 <b>ADMIN DASHBOARD</b> 🌟\n"
            "╚══════════════════════╝\n\n"
            f"👑 Welcome back, <b>{user.first_name}</b>!\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔧 <b>Quick Commands</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🛠 /admin — Open Admin Panel\n"
            "📋 /listsubs — View all subscribers\n"
            "📣 /broadcast — Message all users\n"
            "📅 /setjoindate — Set join date manually\n"
            "❓ /help — Full command reference\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "💡 <i>Use /admin for the full interactive panel.</i>",
            parse_mode="HTML"
        )
    else:
        keyboard = [
            [InlineKeyboardButton("📋 My Subscriptions", callback_data="my_subscription")],
            [InlineKeyboardButton("💳 Buy Subscription", callback_data="pay_start")],
        ]
        await update.message.reply_text(
            "╔══════════════════════╗\n"
            "   ✨ <b>WELCOME ABOARD!</b> ✨\n"
            "╚══════════════════════╝\n\n"
            f"👋 Hello, <b>{user.first_name}</b>! Great to have you here!\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🎯 <b>What can I do for you?</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "💳 Buy access to premium channels\n"
            "📋 Check your active subscriptions\n"
            "⏳ See how much time is remaining\n"
            "🔔 Get notified before expiry\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "👇 <b>Tap a button below to get started!</b>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )


# ─────────────────────────────────────────────
#  /mysubs — User subscription list
# ─────────────────────────────────────────────

async def my_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    subs = db.get_user_subscriptions(user.id)
    if not subs:
        await update.message.reply_text(
            "╔══════════════════════╗\n"
            "   📭 <b>NO SUBSCRIPTIONS</b>\n"
            "╚══════════════════════╝\n\n"
            "😔 You have no active subscriptions yet.\n\n"
            "💳 Tap /start and use <b>Buy Subscription</b> to get access!",
            parse_mode="HTML"
        )
        return
    text = (
        "╔══════════════════════╗\n"
        "   📋 <b>MY SUBSCRIPTIONS</b>\n"
        "╚══════════════════════╝\n\n"
    )
    for sub in subs:
        expiry    = parse_dt(sub["expiry_date"])
        join_date = parse_dt(sub.get("join_date") or sub.get("created_at") or sub["expiry_date"])
        emoji     = status_emoji(expiry)
        text += (
            f"{emoji} <b>{sub.get('channel_name', 'Unknown')}</b>\n"
            f"   🗓 Joined:    {to_ist(join_date).strftime('%d %b %Y %H:%M')} IST\n"
            f"   📅 Expires:   {to_ist(expiry).strftime('%d %b %Y %H:%M')} IST\n"
            f"   ⏳ Remaining: {fmt_remaining(expiry)}\n\n"
        )
    text += "━━━━━━━━━━━━━━━━━━━━━━━━━"
    await update.message.reply_text(text, parse_mode="HTML")


# ─────────────────────────────────────────────
#  /help
# ─────────────────────────────────────────────

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text(
            "📋 <b>Available Commands</b>\n\n"
            "🔹 /start — Welcome screen & buy subscription\n"
            "🔹 /mysubs — View your subscriptions",
            parse_mode="HTML"
        )
        return
    await update.message.reply_text(
        "╔══════════════════════╗\n"
        "   ❓ <b>COMMAND REFERENCE</b>\n"
        "╚══════════════════════╝\n\n"
        "━━━ 📢 <b>Channel Management</b> ━━━\n"
        "➕ /addchannel &lt;id&gt; &lt;months&gt; &lt;name&gt;\n"
        "   <i>Register a channel for tracking</i>\n\n"
        "➖ /removechannel &lt;id&gt;\n"
        "   <i>Stop managing a channel</i>\n\n"
        "━━━ 👥 <b>Subscription Management</b> ━━━\n"
        "✅ /addsub &lt;user_id&gt; &lt;channel_id&gt; &lt;months&gt;\n"
        "   <i>Manually add a subscription</i>\n\n"
        "❌ /removesub &lt;user_id&gt; &lt;channel_id&gt;\n"
        "   <i>Remove sub &amp; kick user</i>\n\n"
        "📅 /setjoindate &lt;user_id&gt; &lt;channel_id&gt; &lt;DD-MM-YYYY&gt;\n"
        "   <i>Set or correct a user's join date</i>\n\n"
        "📋 /listsubs [channel_id]\n"
        "   <i>List all or per-channel subscribers</i>\n\n"
        "━━━ 🛠 <b>Admin Panel</b> ━━━\n"
        "🖥 /admin — Interactive admin panel\n\n"
        "━━━ 📣 <b>Broadcast</b> ━━━\n"
        "📣 /broadcast &lt;message&gt;\n"
        "   <i>Send a message to all subscribers</i>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode="HTML"
    )


# ─────────────────────────────────────────────
#  /admin — Admin Panel
# ─────────────────────────────────────────────

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Access denied.")
        return
    subs     = db.get_all_subscriptions()
    channels = db.get_managed_channels()
    pending  = db.get_all_pending()
    payments = db.get_pending_payment_requests()
    active   = [s for s in subs if parse_dt(s["expiry_date"]) > now_utc()]
    text = (
        "🛠 <b>Admin Panel</b>\n\n"
        f"👥 Total active subscribers: <b>{len(active)}</b>\n"
        f"📢 Managed channels: <b>{len(channels)}</b>\n"
        f"⏳ Pending join approvals: <b>{len(pending)}</b>\n"
        f"💳 Pending payments: <b>{len(payments)}</b>\n\n"
        "Choose an action:"
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=owner_panel_keyboard())


# ─────────────────────────────────────────────
#  PAYMENT FLOW — User side
# ─────────────────────────────────────────────

async def _pay_show_channels(query, context):
    """Step 1 — user picks a channel."""
    channels = db.get_managed_channels()
    if not channels:
        await query.edit_message_text(
            "⚠️ No premium channels available right now.\nPlease check back later!",
            parse_mode="HTML"
        )
        return
    keyboard = [
        [InlineKeyboardButton(f"📢 {ch['name']}", callback_data=f"pay_ch_{ch['channel_id']}")]
        for ch in channels
    ]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="pay_back_home")])
    await query.edit_message_text(
        "╔══════════════════════╗\n"
        "   💳 <b>BUY SUBSCRIPTION</b>\n"
        "╚══════════════════════╝\n\n"
        "📢 <b>Step 1 — Choose a Channel</b>\n\n"
        "Select the premium channel you want access to:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def _pay_show_plans(query, context, channel_id: str):
    """Step 2 — user picks a plan."""
    ch = db.get_channel_info(channel_id)
    ch_name = ch["name"] if ch else channel_id
    context.user_data["pay_channel_id"]   = channel_id
    context.user_data["pay_channel_name"] = ch_name

    plan_rows = []
    for label, months, price in PLANS:
        plan_rows.append([InlineKeyboardButton(
            f"{'⭐' if months == 12 else '📅'} {label} — ₹{price}",
            callback_data=f"pay_plan_{channel_id}_{months}_{price}"
        )])
    plan_rows.append([InlineKeyboardButton("🔙 Back", callback_data="pay_start")])

    await query.edit_message_text(
        "╔══════════════════════╗\n"
        "   💳 <b>BUY SUBSCRIPTION</b>\n"
        "╚══════════════════════╝\n\n"
        f"📢 Channel: <b>{ch_name}</b>\n\n"
        "📅 <b>Step 2 — Choose a Plan</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(plan_rows)
    )


async def _pay_show_qr(query, context, channel_id: str, months: int, price: int):
    """Step 3 — show UPI QR and instructions."""
    ch_name = context.user_data.get("pay_channel_name", channel_id)
    plan_label = next((l for l, m, p in PLANS if m == months and p == price), f"{months}M")

    context.user_data["pay_months"] = months
    context.user_data["pay_price"]  = price
    context.user_data["pay_awaiting_screenshot"] = True

    caption = (
        "╔══════════════════════╗\n"
        "   💳 <b>PAYMENT DETAILS</b>\n"
        "╚══════════════════════╝\n\n"
        f"📢 Channel : <b>{ch_name}</b>\n"
        f"📅 Plan    : <b>{plan_label}</b>\n"
        f"💰 Amount  : <b>₹{price}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📋 <b>STEP-BY-STEP PAYMENT GUIDE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>STEP 1 — Open Your UPI App</b>\n"
        "┗ Open any UPI app on your phone:\n"
        "   Google Pay / PhonePe / Paytm\n"
        "   BHIM / any banking UPI app\n\n"
        "<b>STEP 2 — Pay Using QR or UPI ID</b>\n"
        "┣ 📷 <b>Option A:</b> Scan the QR code above\n"
        f"┗ 🔤 <b>Option B:</b> Pay to UPI ID: <code>{UPI_ID}</code>\n\n"
        "<b>STEP 3 — Enter the Exact Amount</b>\n"
        f"┗ 💵 Type exactly: <b>₹{price}</b>\n"
        "   ⚠️ Wrong amount = request rejected\n\n"
        "<b>STEP 4 — Add a Note (Optional)</b>\n"
        "┗ 📝 Write your Telegram username\n"
        "   in the payment note / remarks field\n\n"
        "<b>STEP 5 — Complete the Payment</b>\n"
        "┗ ✅ Confirm and finish the transaction\n\n"
        "<b>STEP 6 — Send Screenshot Here</b>\n"
        "┗ 📸 Take a screenshot of the\n"
        "   <b>payment success screen</b> and\n"
        "   send it in <b>this chat right now</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⏳ <b>What happens after screenshot:</b>\n"
        "• Admin verifies your payment\n"
        "• You receive a private invite link\n"
        "• Link is valid for <b>1 hour only — join fast!</b>\n\n"
        "⚠️ <i>Do NOT close this chat.\n"
        "Do NOT send the screenshot multiple times.</i>"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data="pay_cancel")
    ]])

    try:
        # Delete old message and send QR as photo
        await query.message.delete()
        await query.message.chat.send_photo(
            photo=QR_FILE_ID,
            caption=caption,
            parse_mode="HTML",
            reply_markup=keyboard
        )
    except Exception:
        # Fallback: just edit if delete fails
        await query.edit_message_text(
            caption + f"\n\n📷 <i>(QR code not set — pay to UPI ID above)</i>",
            parse_mode="HTML",
            reply_markup=keyboard
        )


# ─────────────────────────────────────────────
#  PAYMENT — User sends screenshot
# ─────────────────────────────────────────────

async def handle_user_payment_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles screenshot sent by user during payment flow."""
    user = update.effective_user

    if not context.user_data.get("pay_awaiting_screenshot"):
        return   # not in payment flow

    channel_id = context.user_data.get("pay_channel_id")
    channel_name = context.user_data.get("pay_channel_name", channel_id)
    months     = context.user_data.get("pay_months")
    price      = context.user_data.get("pay_price")

    if not all([channel_id, months, price]):
        await update.message.reply_text(
            "⚠️ Session expired. Please start again with /start.",
            parse_mode="HTML"
        )
        context.user_data.clear()
        return

    # Check if already has pending request for this channel
    existing = db.get_user_pending_payment(user.id, channel_id)
    if existing:
        await update.message.reply_text(
            "⏳ You already have a <b>pending payment request</b> for this channel.\n\n"
            "Please wait for admin approval.",
            parse_mode="HTML"
        )
        context.user_data.clear()
        return

    photo = update.message.photo[-1]
    screenshot_file_id = photo.file_id
    username = user.username or user.first_name or str(user.id)

    # Save payment request
    request_id = db.add_payment_request(
        user_id=user.id,
        channel_id=channel_id,
        channel_name=channel_name,
        username=username,
        first_name=user.first_name or "",
        months=months,
        amount=price,
        screenshot_file_id=screenshot_file_id
    )

    context.user_data.clear()

    # Confirm to user
    await update.message.reply_text(
        "╔══════════════════════╗\n"
        "   ✅ <b>PAYMENT SUBMITTED!</b>\n"
        "╚══════════════════════╝\n\n"
        f"🆔 Request ID: <code>{request_id}</code>\n\n"
        "📸 Your payment screenshot has been received.\n\n"
        "⏳ <b>What happens next?</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "1️⃣ Admin will verify your payment\n"
        f"2️⃣ You'll receive a <b>channel invite link</b>\n"
        "3️⃣ The link is valid for <b>1 hour only</b> — join quickly!\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "💬 Contact admin if you don't hear back within 30 minutes.",
        parse_mode="HTML"
    )

    # Notify owner with Approve / Reject
    plan_label = next((l for l, m, p in PLANS if m == months and p == price), f"{months}M")
    user_link = f'<a href="tg://user?id={user.id}">{user.first_name or username}</a>'
    owner_caption = (
        "╔══════════════════════╗\n"
        "   💳 <b>NEW PAYMENT REQUEST</b>\n"
        "╚══════════════════════╝\n\n"
        f"🆔 Request ID: <code>{request_id}</code>\n\n"
        f"👤 Name: {user_link}\n"
        f"🔖 Username: @{username}\n"
        f"🆔 User ID: <code>{user.id}</code>\n"
        f"📢 Channel: <b>{channel_name}</b>\n"
        f"📅 Plan: <b>{plan_label}</b>\n"
        f"💰 Amount: <b>₹{price}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "✅ Approve → create invite link & activate subscription\n"
        "❌ Reject → notify user, no access"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"pmt_approve_{request_id}"),
        InlineKeyboardButton("❌ Reject",  callback_data=f"pmt_reject_{request_id}"),
    ]])

    try:
        await context.bot.send_photo(
            chat_id=Config.OWNER_ID,
            photo=screenshot_file_id,
            caption=owner_caption,
            parse_mode="HTML",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.warning(f"Could not notify owner about payment: {e}")


# ─────────────────────────────────────────────
#  PAYMENT APPROVE / REJECT — Owner side
# ─────────────────────────────────────────────

async def _approve_payment(query, context, request_id: str):
    req = db.get_payment_request(request_id)
    if not req:
        await query.edit_message_caption("⚠️ Payment request not found or already resolved.")
        return
    if req["status"] != "pending":
        await query.edit_message_caption(
            f"⚠️ This request was already <b>{req['status']}</b>.",
            parse_mode="HTML"
        )
        return

    uid         = req["user_id"]
    channel_id  = req["channel_id"]
    channel_name = req["channel_name"]
    months      = req["months"]
    username    = req.get("username", str(uid))
    first_name  = req.get("first_name", "")
    price       = req.get("amount", 0)
    plan_label  = next((l for l, m, p in PLANS if m == months and p == price), f"{months}M")

    join_date = now_utc()
    expiry    = join_date + timedelta(days=30 * months)

    # Check if already has active sub (renewal case)
    existing = db.get_subscription(uid, channel_id)
    if existing:
        current_expiry = parse_dt(existing["expiry_date"])
        base   = current_expiry if current_expiry > now_utc() else join_date
        expiry = base + timedelta(days=30 * months)
        db.update_subscription_expiry(uid, channel_id, expiry, join_date=join_date)
        is_renewal = True
    else:
        db.add_subscription(uid, channel_id, expiry, username, join_date=join_date,
                            months=months, channel_name=channel_name)
        is_renewal = False

    # Mark payment as approved
    db.update_payment_status(request_id, "approved")

    # Generate one-time invite link (1-hour expiry)
    try:
        invite_url = await create_invite_and_schedule_revoke(
            context.bot, context.job_queue, channel_id, uid
        )
    except Exception as e:
        logger.error(f"Could not create invite link: {e}")
        invite_url = None

    # Notify user
    caption_prefix = "🔄 <b>Subscription Renewed!</b>" if is_renewal else "🎉 <b>Subscription Approved!</b>"
    user_msg = (
        f"{caption_prefix}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📢 Channel: <b>{channel_name}</b>\n"
        f"📅 Plan: <b>{plan_label}</b>\n"
        f"📆 Expires: <b>{to_ist(expiry).strftime('%d %b %Y %H:%M')} IST</b>\n"
        f"⏳ Duration: <b>{fmt_remaining(expiry)}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    if invite_url:
        user_msg += (
            "🔗 <b>Your Channel Invite Link:</b>\n"
            f"<a href=\"{invite_url}\">{invite_url}</a>\n\n"
            "⚠️ <b>This link expires in 1 hour — join now!</b>\n"
            "🔒 It's a one-time link, don't share it."
        )
    else:
        user_msg += "⚠️ Could not generate invite link. Contact admin for manual access."

    try:
        await context.bot.send_message(uid, user_msg, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Could not notify user {uid}: {e}")

    # Update owner message
    owner_confirm = (
        f"✅ <b>Payment Approved</b>\n\n"
        f"🆔 Request: <code>{request_id}</code>\n"
        f"👤 @{username} (<code>{uid}</code>)\n"
        f"📢 {channel_name} | {plan_label}\n"
        f"💰 ₹{price}\n\n"
        f"🔗 Invite link sent to user.\n"
        f"⏱ Link auto-revokes in 1 hour."
    )
    try:
        await query.edit_message_caption(owner_confirm, parse_mode="HTML")
    except Exception:
        await query.message.reply_text(owner_confirm, parse_mode="HTML")


async def _reject_payment(query, context, request_id: str):
    req = db.get_payment_request(request_id)
    if not req:
        await query.edit_message_caption("⚠️ Payment request not found or already resolved.")
        return
    if req["status"] != "pending":
        await query.edit_message_caption(
            f"⚠️ This request was already <b>{req['status']}</b>.",
            parse_mode="HTML"
        )
        return

    uid      = req["user_id"]
    username = req.get("username", str(uid))
    channel_name = req["channel_name"]
    price    = req.get("amount", 0)

    db.update_payment_status(request_id, "rejected")

    try:
        await context.bot.send_message(
            uid,
            "╔══════════════════════╗\n"
            "   ❌ <b>PAYMENT REJECTED</b>\n"
            "╚══════════════════════╝\n\n"
            f"📢 Channel: <b>{channel_name}</b>\n"
            f"💰 Amount: <b>₹{price}</b>\n\n"
            "Your payment could not be verified.\n\n"
            "❓ <b>Possible reasons:</b>\n"
            "• Screenshot unclear or invalid\n"
            "• Wrong amount paid\n"
            "• Payment not received\n\n"
            "💬 Contact admin if you believe this is a mistake.",
            parse_mode="HTML"
        )
    except Exception:
        pass

    owner_confirm = (
        f"❌ <b>Payment Rejected</b>\n\n"
        f"🆔 Request: <code>{request_id}</code>\n"
        f"👤 @{username} (<code>{uid}</code>)\n"
        f"📢 {channel_name} | ₹{price}\n\n"
        "User has been notified."
    )
    try:
        await query.edit_message_caption(owner_confirm, parse_mode="HTML")
    except Exception:
        await query.message.reply_text(owner_confirm, parse_mode="HTML")


# ─────────────────────────────────────────────
#  CHAT MEMBER HANDLER
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

        join_date = result.date
        if join_date.tzinfo is None:
            join_date = join_date.replace(tzinfo=timezone.utc)

        username = user.username or user.first_name or str(user.id)

        # ── Check if user already has an active subscription ──────────
        # This happens when user joins via an invite link sent after payment
        # approval. In that case the subscription is already saved — no need
        # for another pending approval; just log and return.
        existing_sub = db.get_subscription(user.id, channel_id)
        if existing_sub:
            logger.info(
                f"User {user.id} joined {channel_id} — subscription already active "
                f"(plan: {existing_sub.get('months', '?')}M, "
                f"expiry: {existing_sub.get('expiry_date', '?')}). Skipping pending."
            )
            # Notify owner silently so they're aware the user joined
            try:
                expiry    = parse_dt(existing_sub["expiry_date"])
                jd        = parse_dt(existing_sub.get("join_date") or existing_sub.get("created_at") or existing_sub["expiry_date"])
                ch_name   = existing_sub.get("channel_name", chat.title)
                await context.bot.send_message(
                    Config.OWNER_ID,
                    "✅ <b>Member Joined — Subscription Active</b>\n\n"
                    f"👤 Name: <b>{user.first_name}</b>\n"
                    f"🔖 Username: @{username}\n"
                    f"🆔 User ID: <code>{user.id}</code>\n"
                    f"📢 Channel: <b>{chat.title}</b>\n"
                    f"🕐 Joined: <b>{to_ist(join_date).strftime('%d %b %Y %H:%M')} IST</b>\n"
                    f"📆 Expires: <b>{to_ist(expiry).strftime('%d %b %Y %H:%M')} IST</b>\n"
                    f"⏳ Remaining: <b>{fmt_remaining(expiry)}</b>\n\n"
                    "✅ <i>No action needed — subscription was already approved.</i>",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.warning(f"Could not send join notice to owner: {e}")
            return
        # ─────────────────────────────────────────────────────────────

        db.add_pending(
            user_id=user.id,
            channel_id=channel_id,
            username=username,
            first_name=user.first_name or "",
            join_date=join_date,
            channel_name=chat.title,
        )
        logger.info(f"Pending approval: user {user.id} joined {channel_id}")

        approve_cb = f"apr_approve_{user.id}_{channel_id}"
        reject_cb  = f"apr_reject_{user.id}_{channel_id}"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=approve_cb),
            InlineKeyboardButton("❌ Reject",  callback_data=reject_cb),
        ]])

        text = (
            "🔔 <b>New Member — Pending Approval</b>\n\n"
            f"👤 Name: <b>{user.first_name}</b>\n"
            f"🔖 Username: @{username}\n"
            f"🆔 User ID: <code>{user.id}</code>\n"
            f"📢 Channel: <b>{chat.title}</b>\n"
            f"🕐 Joined: <b>{to_ist(join_date).strftime('%d %b %Y %H:%M')} IST</b>\n\n"
            "⚠️ This user joined <b>without going through the payment flow</b>.\n"
            "Please verify payment manually and approve or reject."
        )
        await context.bot.send_message(
            Config.OWNER_ID, text, parse_mode="HTML", reply_markup=keyboard
        )

    elif old_status == ChatMember.MEMBER and new_status in [ChatMember.LEFT, ChatMember.BANNED]:
        channel_id = str(chat.id)
        if db.is_managed_channel(channel_id):
            db.deactivate_subscription(user.id, channel_id)
            db.remove_pending(user.id, channel_id)
            logger.info(f"User {user.id} left {channel_id}, subscription deactivated")


# ─────────────────────────────────────────────
#  CALLBACK HANDLER
# ─────────────────────────────────────────────

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user

    # ── PAYMENT FLOW — User ────────────────────────────────

    if data == "pay_start":
        await _pay_show_channels(query, context)

    elif data == "pay_back_home":
        keyboard = [
            [InlineKeyboardButton("📋 My Subscriptions", callback_data="my_subscription")],
            [InlineKeyboardButton("💳 Buy Subscription", callback_data="pay_start")],
        ]
        await query.edit_message_text(
            "╔══════════════════════╗\n"
            "   ✨ <b>WELCOME ABOARD!</b> ✨\n"
            "╚══════════════════════╝\n\n"
            f"👋 Hello, <b>{user.first_name}</b>!\n\n"
            "👇 <b>Tap a button below to get started!</b>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )

    elif data.startswith("pay_ch_"):
        channel_id = data[len("pay_ch_"):]
        await _pay_show_plans(query, context, channel_id)

    elif data.startswith("pay_plan_"):
        # pay_plan_{channel_id}_{months}_{price}
        rest   = data[len("pay_plan_"):]
        parts  = rest.rsplit("_", 2)
        channel_id = parts[0]
        months     = int(parts[1])
        price      = int(parts[2])
        await _pay_show_qr(query, context, channel_id, months, price)

    elif data == "pay_cancel":
        context.user_data.clear()
        await query.message.delete()
        await query.message.chat.send_message(
            "❌ Payment cancelled.\n\nTap /start to begin again.",
            parse_mode="HTML"
        )

    # ── PAYMENT — Owner Approve / Reject ──────────────────

    elif data.startswith("pmt_approve_") and is_owner(user.id):
        request_id = data[len("pmt_approve_"):]
        await _approve_payment(query, context, request_id)

    elif data.startswith("pmt_reject_") and is_owner(user.id):
        request_id = data[len("pmt_reject_"):]
        await _reject_payment(query, context, request_id)

    # ── Admin: Payment Requests List ──────────────────────

    elif data == "ap_payments" and is_owner(user.id):
        await _show_payment_requests(query)

    # ── User: My Subscriptions ─────────────────────────────

    elif data == "my_subscription":
        subs = db.get_user_subscriptions(user.id)
        if not subs:
            keyboard = [[InlineKeyboardButton("💳 Buy Subscription", callback_data="pay_start")]]
            await query.edit_message_text(
                "❌ You have no active subscriptions yet.\n\n"
                "Tap below to purchase access!",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        text = "📋 <b>Your Subscriptions</b>\n\n"
        for sub in subs:
            expiry    = parse_dt(sub["expiry_date"])
            join_date = parse_dt(sub.get("join_date") or sub.get("created_at") or sub["expiry_date"])
            emoji = status_emoji(expiry)
            text += (
                f"{emoji} <b>{sub.get('channel_name', 'Unknown')}</b>\n"
                f"   🗓 Joined:   {to_ist(join_date).strftime('%d %b %Y %H:%M')} IST\n"
                f"   📅 Expires:  {to_ist(expiry).strftime('%d %b %Y %H:%M')} IST\n"
                f"   ⏳ Remaining: {fmt_remaining(expiry)}\n\n"
            )
        await query.edit_message_text(text, parse_mode="HTML")

    # ── Admin: Back ────────────────────────────────────────

    elif data == "ap_back" and is_owner(user.id):
        subs     = db.get_all_subscriptions()
        channels = db.get_managed_channels()
        pending  = db.get_all_pending()
        payments = db.get_pending_payment_requests()
        active   = [s for s in subs if parse_dt(s["expiry_date"]) > now_utc()]
        text = (
            "🛠 <b>Admin Panel</b>\n\n"
            f"👥 Total active subscribers: <b>{len(active)}</b>\n"
            f"📢 Managed channels: <b>{len(channels)}</b>\n"
            f"⏳ Pending join approvals: <b>{len(pending)}</b>\n"
            f"💳 Pending payments: <b>{len(payments)}</b>\n\n"
            "Choose an action:"
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=owner_panel_keyboard())

    elif data == "ap_pending" and is_owner(user.id):
        await _show_pending_list(query)

    elif data.startswith("apr_approve_") and is_owner(user.id):
        parts = data[len("apr_approve_"):].split("_", 1)
        uid, channel_id = int(parts[0]), parts[1]
        pending = db.get_pending(uid, channel_id)
        if not pending:
            await query.edit_message_text("⚠️ This approval request no longer exists.")
            return
        context.user_data["apr_uid"]          = uid
        context.user_data["apr_channel_id"]   = channel_id
        context.user_data["apr_channel_name"] = pending.get("channel_name", channel_id)
        context.user_data["apr_username"]      = pending.get("username", str(uid))
        context.user_data["apr_join_date"]     = pending.get("join_date")
        context.user_data["awaiting"]          = "apr_months"

        uname = pending.get("username", str(uid))
        ch    = pending.get("channel_name", channel_id)
        await query.edit_message_text(
            f"✅ <b>Approving subscription</b>\n\n"
            f"👤 @{uname} (<code>{uid}</code>)\n"
            f"📢 {ch}\n\n"
            "Select subscription duration:",
            parse_mode="HTML",
            reply_markup=month_picker_keyboard(uid, channel_id, "apr_months_")
        )

    elif data.startswith("apr_months_") and is_owner(user.id):
        rest   = data[len("apr_months_"):]
        parts  = rest.rsplit("_", 1)
        months = int(parts[1])
        uid_ch = parts[0].split("_", 1)
        uid, channel_id = int(uid_ch[0]), uid_ch[1]

        context.user_data["apr_uid"]        = uid
        context.user_data["apr_channel_id"] = channel_id
        context.user_data["apr_months"]     = months
        context.user_data["awaiting"]       = "apr_photo"

        ch_name = context.user_data.get("apr_channel_name", channel_id)
        uname   = context.user_data.get("apr_username", str(uid))

        await query.edit_message_text(
            f"📸 <b>Payment Screenshot</b>\n\n"
            f"👤 @{uname} | 📢 {ch_name} | 🗓 {months} month(s)\n\n"
            "Send the payment screenshot now, or tap <b>Skip</b> to confirm without one.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⏭ Skip Screenshot", callback_data="apr_skip_photo")
            ]])
        )

    elif data == "apr_skip_photo" and is_owner(user.id):
        await _finish_approval(update, context, photo_file_id=None)

    elif data.startswith("apr_reject_") and is_owner(user.id):
        parts      = data[len("apr_reject_"):].split("_", 1)
        uid, channel_id = int(parts[0]), parts[1]
        pending = db.get_pending(uid, channel_id)
        ch_name = pending.get("channel_name", channel_id) if pending else channel_id
        uname   = pending.get("username", str(uid)) if pending else str(uid)

        try:
            await context.bot.ban_chat_member(channel_id, uid)
            await context.bot.unban_chat_member(channel_id, uid)
        except Exception as e:
            logger.warning(f"Could not kick {uid} from {channel_id}: {e}")

        try:
            await context.bot.send_message(
                uid,
                "❌ <b>Access Rejected</b>\n\n"
                f"Your request to join <b>{ch_name}</b> was not approved.\n"
                "Please contact the admin if you believe this is a mistake.",
                parse_mode="HTML"
            )
        except Exception:
            pass

        db.remove_pending(uid, channel_id)
        await query.edit_message_text(
            f"❌ Rejected and kicked @{uname} (<code>{uid}</code>) from <b>{ch_name}</b>.",
            parse_mode="HTML"
        )

    elif data == "ap_users" and is_owner(user.id):
        await _show_users_list(query)

    elif data.startswith("ap_users_page_") and is_owner(user.id):
        page = int(data.split("_")[-1])
        await _show_users_list(query, page=page)

    elif data.startswith("ap_user_") and is_owner(user.id):
        uid = int(data.split("_")[2])
        await _show_user_detail(query, uid)

    elif data == "ap_channels" and is_owner(user.id):
        await _show_channels(query)

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
        context.user_data["add_channel_id"]   = channel_id
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

    elif data.startswith("ap_extend_") and is_owner(user.id):
        parts      = data.split("_")
        uid        = int(parts[2])
        channel_id = "_".join(parts[3:])
        context.user_data["add_channel_id"]   = channel_id
        context.user_data["add_user_id"]      = uid
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

    elif data.startswith("ap_removesub_") and is_owner(user.id):
        parts      = data.split("_")
        uid        = int(parts[2])
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

    elif data == "ap_broadcast_prompt" and is_owner(user.id):
        await query.edit_message_text(
            "📣 <b>Broadcast</b>\n\nUse command:\n<code>/broadcast Your message here</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="ap_back")]])
        )

    elif data == "ap_skip_photo" and is_owner(user.id):
        await _finish_add_sub(update, context, photo_file_id=None)

    # ── /setpayment — Update UPI ID ───────────────────────
    elif data == "setpay_upi" and is_owner(user.id):
        context.user_data["awaiting"] = "setpay_upi"
        await query.edit_message_text(
            "💳 <b>Update UPI ID</b>\n\n"
            f"Current: <code>{Config.UPI_ID or 'Not set'}</code>\n\n"
            "Send your new UPI ID now:\n"
            "<i>Example: yourname@upi</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="setpay_cancel")
            ]])
        )

    elif data == "setpay_qr" and is_owner(user.id):
        context.user_data["awaiting"] = "setpay_qr"
        await query.edit_message_text(
            "🖼 <b>Update QR Code</b>\n\n"
            "Send your new UPI QR code image now.\n\n"
            "💡 <i>The bot will store it automatically.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="setpay_cancel")
            ]])
        )

    elif data == "setpay_cancel" and is_owner(user.id):
        context.user_data.pop("awaiting", None)
        current_upi = Config.UPI_ID or "Not set"
        qr_status   = "✅ Set" if Config.UPI_QR_FILE_ID else "❌ Not set"
        await query.edit_message_text(
            "╔══════════════════════╗\n"
            "   ⚙️ <b>PAYMENT SETTINGS</b>\n"
            "╚══════════════════════╝\n\n"
            f"💳 Current UPI ID: <code>{current_upi}</code>\n"
            f"🖼 QR Code: {qr_status}\n\n"
            "What do you want to update?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Update UPI ID",  callback_data="setpay_upi")],
                [InlineKeyboardButton("🖼 Update QR Code", callback_data="setpay_qr")],
            ])
        )


# ─────────────────────────────────────────────
#  ADMIN PANEL HELPERS
# ─────────────────────────────────────────────

async def _show_payment_requests(query):
    payments = db.get_pending_payment_requests()
    if not payments:
        await query.edit_message_text(
            "✅ No pending payment requests.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="ap_back")]])
        )
        return
    text = f"💳 <b>Pending Payments</b> ({len(payments)})\n\n"
    keyboard = []
    for p in payments:
        plan_label = next((l for l, m, pr in PLANS if m == p["months"] and pr == p["amount"]), f"{p['months']}M")
        uname = p.get("username", str(p["user_id"]))
        text += (
            f"🆔 <code>{p['request_id']}</code>\n"
            f"👤 @{uname} | 📢 {p['channel_name']}\n"
            f"📅 {plan_label} | 💰 ₹{p['amount']}\n\n"
        )
        keyboard.append([
            InlineKeyboardButton(f"✅ {p['request_id']}", callback_data=f"pmt_approve_{p['request_id']}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"pmt_reject_{p['request_id']}"),
        ])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="ap_back")])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def _show_pending_list(query):
    pending_list = db.get_all_pending()
    if not pending_list:
        await query.edit_message_text(
            "✅ No pending approvals.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="ap_back")]])
        )
        return
    text = f"⏳ <b>Pending Approvals</b> ({len(pending_list)})\n\n"
    keyboard = []
    for p in pending_list:
        jd    = parse_dt(p["join_date"])
        uname = p.get("username", str(p["user_id"]))
        ch    = p.get("channel_name", p["channel_id"])
        text += (
            f"👤 @{uname} (<code>{p['user_id']}</code>)\n"
            f"   📢 {ch} | 🕐 {to_ist(jd).strftime('%d %b %H:%M')} IST\n\n"
        )
        keyboard.append([
            InlineKeyboardButton(
                f"✅ {uname[:12]} — Approve",
                callback_data=f"apr_approve_{p['user_id']}_{p['channel_id']}"
            ),
            InlineKeyboardButton(
                "❌ Reject",
                callback_data=f"apr_reject_{p['user_id']}_{p['channel_id']}"
            ),
        ])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="ap_back")])
    await query.edit_message_text(
        text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def _show_users_list(query, page: int = 0):
    PAGE_SIZE = 8
    subs  = db.get_all_subscriptions()
    seen  = {}
    for s in subs:
        uid    = s["user_id"]
        expiry = parse_dt(s["expiry_date"])
        if uid not in seen or expiry < parse_dt(seen[uid]["expiry_date"]):
            seen[uid] = s
    users = sorted(seen.values(), key=lambda s: parse_dt(s["expiry_date"]))
    total = len(users)
    start = page * PAGE_SIZE
    page_users = users[start:start + PAGE_SIZE]

    if not page_users:
        await query.edit_message_text(
            "📭 No subscribers found.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="ap_back")]])
        )
        return

    text     = f"👥 <b>Subscribers</b> ({total} total) — Page {page + 1}\n\n"
    keyboard = []
    for sub in page_users:
        expiry = parse_dt(sub["expiry_date"])
        emoji  = status_emoji(expiry)
        label  = f"{emoji} {sub.get('username', str(sub['user_id']))} — {fmt_remaining(expiry)}"
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

    text           = f"👤 <b>User Detail</b> — <code>{uid}</code>\n\n"
    action_buttons = []
    for sub in all_subs:
        expiry     = parse_dt(sub["expiry_date"])
        join_date  = parse_dt(sub.get("join_date") or sub.get("created_at") or sub["expiry_date"])
        is_active  = sub.get("is_active", True)
        emoji      = status_emoji(expiry) if is_active else "⚫"
        ch_name    = sub.get("channel_name", sub["channel_id"])
        channel_id = sub["channel_id"]
        text += (
            f"{emoji} <b>{ch_name}</b>\n"
            f"   🗓 Join Date:   {to_ist(join_date).strftime('%d %b %Y %H:%M')} IST\n"
            f"   📆 Expire Date: {to_ist(expiry).strftime('%d %b %Y %H:%M')} IST\n"
            f"   ⏳ Expires In:  {fmt_remaining(expiry)}\n"
            f"   Status: {'✅ Active' if is_active else '❌ Inactive'}\n\n"
        )
        if is_active:
            action_buttons.append([
                InlineKeyboardButton(f"➕ Extend ({ch_name[:12]})", callback_data=f"ap_extend_{uid}_{channel_id}"),
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
            subs   = db.get_all_subscriptions(ch["channel_id"])
            active = [s for s in subs if parse_dt(s["expiry_date"]) > now_utc()]
            text  += (
                f"• <b>{ch['name']}</b>\n"
                f"  ID: <code>{ch['channel_id']}</code>\n"
                f"  Plan: {ch['plan_months']} month(s) | Active subs: {len(active)}\n\n"
            )
    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="ap_back")]])
    )


# ─────────────────────────────────────────────
#  APPROVAL FLOW FINISH
# ─────────────────────────────────────────────

async def _finish_approval(update_or_query, context: ContextTypes.DEFAULT_TYPE,
                           photo_file_id: str = None):
    data        = context.user_data
    uid         = data["apr_uid"]
    channel_id  = data["apr_channel_id"]
    channel_name = data.get("apr_channel_name", channel_id)
    months      = data["apr_months"]
    username    = data.get("apr_username", str(uid))
    raw_join    = data.get("apr_join_date")

    join_date = parse_dt(raw_join) if raw_join else now_utc()
    expiry    = join_date + timedelta(days=30 * months)

    existing = db.get_subscription(uid, channel_id)
    if existing:
        current_expiry = parse_dt(existing["expiry_date"])
        base   = current_expiry if current_expiry > now_utc() else join_date
        expiry = base + timedelta(days=30 * months)
        db.update_subscription_expiry(uid, channel_id, expiry, join_date=join_date)
        caption = "🔄 <b>Subscription Approved &amp; Extended!</b>"
    else:
        db.add_subscription(uid, channel_id, expiry, username, join_date=join_date)
        caption = "🎉 <b>Subscription Approved!</b>"

    db.remove_pending(uid, channel_id)
    context.user_data.clear()

    try:
        await send_sub_info(
            context.bot, uid,
            username=username, user_id=uid,
            channel_name=channel_name,
            join_date=join_date, expiry=expiry,
            photo_file_id=photo_file_id,
            extra_caption=caption
        )
    except Exception as e:
        logger.warning(f"Could not notify user {uid}: {e}")

    await send_sub_info(
        context.bot, Config.OWNER_ID,
        username=username, user_id=uid,
        channel_name=channel_name,
        join_date=join_date, expiry=expiry,
        photo_file_id=photo_file_id,
        extra_caption=f"✅ <b>Approved — Subscription Active</b>"
    )


# ─────────────────────────────────────────────
#  ADD/EXTEND SUB FLOW FINISH
# ─────────────────────────────────────────────

async def _finish_add_sub(update_or_query, context: ContextTypes.DEFAULT_TYPE,
                          photo_file_id: str = None):
    data         = context.user_data
    uid          = data["add_user_id"]
    channel_id   = data["add_channel_id"]
    channel_name = data["add_channel_name"]
    months       = data["add_months"]
    action       = data.get("add_action", "new")

    user_info = await get_user_info(context, uid)
    username  = (user_info.username or user_info.first_name or str(uid)) if user_info else str(uid)

    if action == "extend":
        existing = db.get_subscription(uid, channel_id)
        if existing:
            current_expiry = parse_dt(existing["expiry_date"])
            base   = current_expiry if current_expiry > now_utc() else now_utc()
        else:
            base = now_utc()
        new_expiry = base + timedelta(days=30 * months)
        db.update_subscription_expiry(uid, channel_id, new_expiry)
        join_date      = parse_dt(existing["join_date"]) if existing and existing.get("join_date") else now_utc()
        caption_prefix = "🔄 <b>Subscription Extended!</b>"
    else:
        new_expiry     = now_utc() + timedelta(days=30 * months)
        join_date      = now_utc()
        db.add_subscription(uid, channel_id, new_expiry, username, join_date=join_date)
        caption_prefix = "🎉 <b>Subscription Added!</b>"

    context.user_data.clear()

    try:
        await send_sub_info(
            context.bot, uid,
            username=username, user_id=uid,
            channel_name=channel_name,
            join_date=join_date, expiry=new_expiry,
            photo_file_id=photo_file_id,
            extra_caption=caption_prefix
        )
    except Exception as e:
        logger.warning(f"Could not notify user {uid}: {e}")

    await send_sub_info(
        context.bot, Config.OWNER_ID,
        username=username, user_id=uid,
        channel_name=channel_name,
        join_date=join_date, expiry=new_expiry,
        photo_file_id=photo_file_id,
        extra_caption=f"✅ <b>Done — {caption_prefix}</b>"
    )

    confirm = (
        f"✅ {'Extended' if action == 'extend' else 'Added'} subscription for "
        f"<code>{uid}</code> (@{username})\n"
        f"📢 {channel_name} — expires <b>{to_ist(new_expiry).strftime('%d %b %Y')}</b>"
    )
    if hasattr(update_or_query, 'callback_query'):
        await update_or_query.effective_message.reply_text(confirm, parse_mode="HTML")
    else:
        await update_or_query.message.reply_text(confirm, parse_mode="HTML")


# ─────────────────────────────────────────────
#  MESSAGE HANDLER — multi-step flows
# ─────────────────────────────────────────────

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return

    awaiting = context.user_data.get("awaiting")
    if not awaiting:
        return

    text = update.message.text.strip()

    if awaiting == "user_id":
        if not text.isdigit():
            await update.message.reply_text("❗ Please send a valid numeric Telegram User ID.")
            return
        uid = int(text)
        context.user_data["add_user_id"] = uid

        existing  = db.get_subscription(uid, context.user_data["add_channel_id"])
        user_info = await get_user_info(context, uid)
        name      = (user_info.first_name or str(uid)) if user_info else str(uid)

        if existing:
            expiry = parse_dt(existing["expiry_date"])
            await update.message.reply_text(
                f"👤 User: <b>{name}</b> (<code>{uid}</code>)\n"
                f"📢 Channel: <b>{context.user_data['add_channel_name']}</b>\n\n"
                f"⚠️ Already subscribed — expires <b>{to_ist(expiry).strftime('%d %b %Y')}</b> ({fmt_remaining(expiry)} left)\n\n"
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

    elif awaiting in ("months", "extend_months"):
        if not text.isdigit() or int(text) < 1:
            await update.message.reply_text("❗ Please send a valid number of months (e.g. 1, 3, 6).")
            return
        context.user_data["add_months"] = int(text)
        context.user_data["awaiting"]   = "photo"

        await update.message.reply_text(
            "📸 <b>Payment Screenshot</b>\n\n"
            "Send the payment screenshot now, or tap <b>Skip</b> to proceed without one.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⏭ Skip Screenshot", callback_data="ap_skip_photo")
            ]])
        )

    elif awaiting == "sjd_months":
        if not text.isdigit() or int(text) < 1:
            await update.message.reply_text("❗ Please send a valid number of months (e.g. 1, 3, 6, 12).")
            return
        context.user_data["sjd_months"] = int(text)
        context.user_data["awaiting"]   = None
        await _finish_set_join_date(update, context)

    elif awaiting == "setpay_upi":
        context.user_data.pop("awaiting", None)
        new_upi = text.strip()
        # Update in-memory config
        Config.UPI_ID = new_upi
        global UPI_ID
        UPI_ID = new_upi
        await update.message.reply_text(
            "✅ <b>UPI ID Updated!</b>\n\n"
            f"💳 New UPI ID: <code>{new_upi}</code>\n\n"
            "⚠️ <i>This is temporary — add it to your .env to make it permanent:</i>\n"
            f"<code>UPI_ID={new_upi}</code>",
            parse_mode="HTML"
        )


async def handle_photo_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Photos from owner (during admin flows) or users (payment screenshots)."""
    user = update.effective_user

    if is_owner(user.id):
        awaiting = context.user_data.get("awaiting")
        photo    = update.message.photo[-1]

        if awaiting == "apr_photo":
            context.user_data["awaiting"] = None
            await _finish_approval(update, context, photo_file_id=photo.file_id)
        elif awaiting == "photo":
            context.user_data["awaiting"] = None
            await _finish_add_sub(update, context, photo_file_id=photo.file_id)
        elif awaiting == "setpay_qr":
            context.user_data.pop("awaiting", None)
            new_file_id = photo.file_id
            # Update in-memory config
            Config.UPI_QR_FILE_ID = new_file_id
            global QR_FILE_ID
            QR_FILE_ID = new_file_id
            await update.message.reply_text(
                "✅ <b>QR Code Updated!</b>\n\n"
                "🖼 New QR code is now active and will be shown to users during payment.\n\n"
                "⚠️ <i>This is temporary — add it to your .env to make it permanent:</i>\n"
                f"<code>UPI_QR_FILE_ID={new_file_id}</code>",
                parse_mode="HTML"
            )
        else:
            # Owner sent a photo outside any flow — ignore
            pass
    else:
        # Non-owner — handle payment screenshot
        await handle_user_payment_screenshot(update, context)


# ─────────────────────────────────────────────
#  /setjoindate
# ─────────────────────────────────────────────

async def set_join_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "❗ <b>Usage:</b> /setjoindate &lt;user_id&gt; &lt;channel_id&gt; &lt;DD-MM-YYYY&gt;\n\n"
            "<b>Example:</b>\n"
            "<code>/setjoindate 123456789 -1001234567890 15-01-2025</code>",
            parse_mode="HTML"
        )
        return

    try:
        uid        = int(args[0])
        channel_id = args[1]
        date_str   = args[2]
        new_join   = datetime.strptime(date_str, "%d-%m-%Y").replace(tzinfo=timezone.utc)
    except ValueError:
        await update.message.reply_text(
            "❗ Invalid arguments.\n"
            "Date must be in <b>DD-MM-YYYY</b> format, e.g. <code>15-01-2025</code>",
            parse_mode="HTML"
        )
        return

    sub = db.get_subscription(uid, channel_id)
    if not sub:
        await update.message.reply_text(
            f"❌ No subscription found for user <code>{uid}</code> in that channel.",
            parse_mode="HTML"
        )
        return

    ch           = db.get_channel_info(channel_id)
    channel_name = ch["name"] if ch else channel_id
    user_info    = await get_user_info(context, uid)
    username     = (user_info.username or user_info.first_name or str(uid)) if user_info else str(uid)
    first_name   = user_info.first_name if user_info else ""

    context.user_data["sjd_uid"]          = uid
    context.user_data["sjd_channel_id"]   = channel_id
    context.user_data["sjd_channel_name"] = channel_name
    context.user_data["sjd_new_join"]     = new_join.isoformat()
    context.user_data["sjd_username"]     = username
    context.user_data["sjd_first_name"]   = first_name
    context.user_data["awaiting"]         = "sjd_months"

    await update.message.reply_text(
        "📅 <b>Set Join Date</b>\n\n"
        f"👤 User: <a href=\"tg://user?id={uid}\">{first_name or username}</a> (<code>{uid}</code>)\n"
        f"📢 Channel: <b>{channel_name}</b>\n"
        f"📆 New Join Date: <b>{to_ist(new_join).strftime('%d %b %Y')} IST</b>\n\n"
        "⏳ How many <b>months</b> is this subscription period?\n"
        "<i>Expiry will be recalculated as: join date + months</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="ap_back")
        ]])
    )


async def _finish_set_join_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data         = context.user_data
    uid          = data["sjd_uid"]
    channel_id   = data["sjd_channel_id"]
    channel_name = data["sjd_channel_name"]
    months       = data["sjd_months"]
    username     = data["sjd_username"]
    first_name   = data["sjd_first_name"]
    new_join     = parse_dt(data["sjd_new_join"])
    new_expiry   = new_join + timedelta(days=30 * months)

    db.set_join_date(uid, channel_id, new_join)
    db.update_subscription_expiry(uid, channel_id, new_expiry, join_date=new_join)
    context.user_data.clear()

    confirm = (
        "✅ <b>Join Date &amp; Expiry Updated!</b>\n\n"
        f"👤 User: <a href=\"tg://user?id={uid}\">{first_name or username}</a> (<code>{uid}</code>)\n"
        f"📢 Channel: <b>{channel_name}</b>\n"
        f"📅 Join Date: <b>{to_ist(new_join).strftime('%d %b %Y')} IST</b>\n"
        f"🗓 Period: <b>{months} month(s)</b>\n"
        f"📆 New Expiry: <b>{to_ist(new_expiry).strftime('%d %b %Y')} IST</b>"
    )
    await update.message.reply_text(confirm, parse_mode="HTML")

    try:
        await send_sub_info(
            context.bot, uid,
            username=username, user_id=uid,
            channel_name=channel_name,
            join_date=new_join, expiry=new_expiry,
            first_name=first_name,
            extra_caption="📅 <b>Your Subscription Has Been Updated</b>"
        )
    except Exception:
        pass


# ─────────────────────────────────────────────
#  OWNER COMMANDS
# ─────────────────────────────────────────────

async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    join_date = now_utc()
    expiry    = join_date + timedelta(days=30 * months)
    user_info = await get_user_info(context, user_id)
    username  = (user_info.username or user_info.first_name) if user_info else str(user_id)
    db.add_subscription(user_id, channel_id, expiry, username, join_date=join_date)

    ch           = db.get_channel_info(channel_id)
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
        expiry = parse_dt(sub["expiry_date"])
        emoji  = status_emoji(expiry)
        text  += (
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
    msg  = " ".join(context.args)
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
        expiry       = parse_dt(sub["expiry_date"])
        channel_name = sub.get("channel_name", sub["channel_id"])
        join_date    = parse_dt(sub.get("join_date") or sub.get("created_at") or sub["expiry_date"])
        user_info    = await get_user_info(context, sub["user_id"])
        first_name   = user_info.first_name if user_info else ""
        try:
            await send_sub_info(
                context.bot, sub["user_id"],
                username=sub.get("username", ""),
                user_id=sub["user_id"],
                channel_name=channel_name,
                join_date=join_date, expiry=expiry,
                first_name=first_name,
                extra_caption="⚠️ <b>Subscription Expiring Soon!</b>\nPlease renew via /start → Buy Subscription."
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
                join_date=join_date, expiry=expiry,
                first_name=first_name,
                extra_caption="⚠️ <b>Expiring Soon — Heads Up</b>"
            )
        except Exception:
            pass

    for sub in db.get_expiring_subscriptions_1d():
        expiry       = parse_dt(sub["expiry_date"])
        channel_name = sub.get("channel_name", sub["channel_id"])
        join_date    = parse_dt(sub.get("join_date") or sub.get("created_at") or sub["expiry_date"])
        user_info    = await get_user_info(context, sub["user_id"])
        first_name   = user_info.first_name if user_info else ""
        try:
            await send_sub_info(
                context.bot, Config.OWNER_ID,
                username=sub.get("username", ""),
                user_id=sub["user_id"],
                channel_name=channel_name,
                join_date=join_date, expiry=expiry,
                first_name=first_name,
                extra_caption="🔔 <b>Expiring in 1 Day — Action Required!</b>"
            )
            db.mark_notified(sub["user_id"], sub["channel_id"], "1d")
        except Exception as e:
            logger.warning(f"Could not send 1d notice for {sub['user_id']}: {e}")

    for sub in db.get_expired_subscriptions():
        expiry       = parse_dt(sub["expiry_date"])
        channel_name = sub.get("channel_name", sub["channel_id"])
        join_date    = parse_dt(sub.get("join_date") or sub.get("created_at") or sub["expiry_date"])
        user_info    = await get_user_info(context, sub["user_id"])
        first_name   = user_info.first_name if user_info else ""
        try:
            await send_sub_info(
                context.bot, sub["user_id"],
                username=sub.get("username", ""),
                user_id=sub["user_id"],
                channel_name=channel_name,
                join_date=join_date, expiry=expiry,
                first_name=first_name,
                extra_caption="❌ <b>Subscription Expired!</b>\nContact the admin to renew via /start → Buy Subscription."
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
                join_date=join_date, expiry=expiry,
                first_name=first_name,
                extra_caption="❌ <b>Expired &amp; Removed from Channel</b>"
            )
        except Exception:
            pass
        db.mark_expired_notified(sub["user_id"], sub["channel_id"])


# ─────────────────────────────────────────────
#  /setpayment — Update UPI ID or QR Code
# ─────────────────────────────────────────────

async def set_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner command to update UPI ID or QR code on the fly."""
    if not is_owner(update.effective_user.id):
        return

    current_upi = Config.UPI_ID or "Not set"
    qr_status   = "✅ Set" if Config.UPI_QR_FILE_ID else "❌ Not set"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Update UPI ID",    callback_data="setpay_upi")],
        [InlineKeyboardButton("🖼 Update QR Code",   callback_data="setpay_qr")],
    ])
    await update.message.reply_text(
        "╔══════════════════════╗\n"
        "   ⚙️ <b>PAYMENT SETTINGS</b>\n"
        "╚══════════════════════╝\n\n"
        f"💳 Current UPI ID: <code>{current_upi}</code>\n"
        f"🖼 QR Code: {qr_status}\n\n"
        "What do you want to update?",
        parse_mode="HTML",
        reply_markup=keyboard
    )


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def main():
    db.init_db()
    app = Application.builder().token(Config.BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",         start))
    app.add_handler(CommandHandler("mysubs",        my_subscriptions))
    app.add_handler(CommandHandler("help",          help_command))
    app.add_handler(CommandHandler("admin",         admin_panel))
    app.add_handler(CommandHandler("addchannel",    add_channel))
    app.add_handler(CommandHandler("removechannel", remove_channel))
    app.add_handler(CommandHandler("addsub",        add_subscription))
    app.add_handler(CommandHandler("removesub",     remove_subscription))
    app.add_handler(CommandHandler("setjoindate",   set_join_date))
    app.add_handler(CommandHandler("listsubs",      list_subs))
    app.add_handler(CommandHandler("broadcast",     broadcast))
    app.add_handler(CommandHandler("setpayment",    set_payment))

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(ChatMemberHandler(chat_member_update, ChatMemberHandler.CHAT_MEMBER))

    # Photo handler — owner admin flows + user payment screenshots
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_input))

    # Text handler — owner only, multi-step flows
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.User(Config.OWNER_ID),
        handle_text_input
    ))

    app.job_queue.run_repeating(check_expiring_subscriptions, interval=43200, first=30)

    logger.info("Bot started...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
