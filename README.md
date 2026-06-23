# 📢 Telegram Subscription Manager Bot

A Telegram bot to manage premium channel subscriptions — auto-tracks joins, sends expiry notifications, and removes expired users.

---

## ✨ Features

- ✅ Auto-detects when a user joins a managed channel
- 📅 Tracks subscription expiry per user per channel
- ⚠️ Sends expiry warning **3 days before** expiry (to user + owner)
- ❌ Auto-removes expired users from the channel
- 📣 Owner broadcast to all subscribers
- 🔧 Full owner panel via `/start`
- 👤 Users can check their own subscription status

---

## 🚀 Setup

### 1. Create a Bot
1. Open Telegram → search `@BotFather`
2. Send `/newbot` and follow the steps
3. Copy your **bot token**

### 2. Get Your Telegram User ID
- Message `@userinfobot` on Telegram
- Copy your **user ID**

### 3. Configure the Bot

```bash
cp .env.example .env
```

Edit `.env`:
```
BOT_TOKEN=your_bot_token_here
OWNER_ID=your_telegram_user_id
```

### 4. Install Dependencies

```bash
pip install -r requirements.txt
```

### 5. Run the Bot

```bash
python bot.py
```

---

## 📢 Adding a Channel

1. Add the bot to your Telegram channel as **Administrator**
2. Give it these permissions:
   - ✅ Add members / Ban users
   - ✅ (optional) Post messages
3. Send this command in your bot DM:

```
/addchannel -1001234567890 1 My Premium Channel
```

Where:
- `-1001234567890` → your channel ID (use `@userinfobot` in your channel or forward a message to get it)
- `1` → subscription plan in **months**
- `My Premium Channel` → display name

---

## 🔧 Owner Commands

| Command | Description |
|---|---|
| `/admin` | Open the interactive admin panel |
| `/addchannel <channel_id> <months> <name>` | Register a channel for subscription tracking |
| `/removechannel <channel_id>` | Stop managing a channel |
| `/addsub <user_id> <channel_id> <months>` | Manually add/extend a subscription |
| `/removesub <user_id> <channel_id>` | Remove a subscription and kick user |
| `/setjoindate <user_id> <channel_id> <DD-MM-YYYY>` | Set or correct a user's join date |
| `/listsubs [channel_id]` | List all (or per-channel) subscriptions |
| `/broadcast <message>` | Send a message to all subscribers |
| `/setpayment` | Update your UPI ID and QR code image |
| `/help` | Show the full command reference |

---

## 👤 User Commands

| Command | Description |
|---|---|
| `/start` | Welcome screen — buy a subscription or check your status |
| `/pay` | Start the buy/renew flow directly: pick channel → pick plan → pay via QR |
| `/mysubs` | View your active subscriptions |
| `/myplan` | Check your active plan and expiry date |
| `/help` | Show available commands |

---

## ⚙️ How It Works

```
User joins channel
       │
       ▼
Bot detects via ChatMemberHandler
       │
       ▼
Subscription stored in SQLite DB
       │
       ▼
Every 12 hours → scheduler checks:
  • Expiring in 3 days? → Warn user + owner
  • Already expired?    → Notify + kick user
```

---

## 🗄️ Database

Uses **SQLite** — no extra database server needed. File is saved as `subscriptions.db` in the bot directory.

---

## 🛡️ Requirements

- Python 3.10+
- Bot must be **admin** in every managed channel
- Bot must be able to DM users (users must have started the bot first)
