# Telegram APK Protection Bot

A Telegram bot that lets users submit APK files for protection. The bot automatically relays APKs to a secondary processing bot, receives the result, and delivers it back to the user — with a live waiting timer throughout.

---

## Features

### Core
- Users send an APK → bot processes it automatically via Bot2 relay
- Live timer message updates every 30 seconds while the user waits
- Processed APK delivered back to the user as a reply to their original message
- Paid / trial user system with daily and monthly token limits
- Max APK size: **20 MB**
- Max subscription: **30 days**
- Monthly cap: **500 APKs per user**
- Unique random submission IDs (always increasing, starting from 1000)
- Rate limit: **10-second cooldown** between submissions per user
- Multiple concurrent users supported

### Bot2 Automated Relay (NEW)
- User sends APK → bot automatically forwards it to a secondary bot (`@android_protect_bot`) using a Telethon user-account session
- Bot waits for the processed APK response from Bot2
- Processed APK is downloaded and delivered directly to the user — no manual admin action needed
- Telethon session is auto-saved to `.env` after first login — no OTP required on subsequent restarts

### Channel Broadcast (NEW)
- Admin posts anything in a configured Telegram channel
- Bot automatically forwards that post to **all registered users** (paid + unpaid)
- Supports any content type: text, photo, video, file, etc.
- Set `CHANNEL_ID` in `.env` to enable (leave blank to disable)

### Admin Broadcast Command (NEW)
- `/broadcast <text>` — admin sends a text message to all registered users
- Reply to any message + `/broadcast` — forwards that message to all users
- Shows live progress and a final sent/blocked/failed summary

### Admin Dashboard
- Full user management via admin-only commands
- Forward APK to admin as fallback if Bot2 is unavailable
- Admin can reply to a forwarded APK with the processed file to deliver manually
- Admin can send text replies to users via the bot

---

## How It Works

### Automated flow (Bot2 relay)

1. User sends an APK file (max 20 MB)
2. Bot checks: size → rate limit → token balance → subscription
3. APK is forwarded to admin (for records) and simultaneously sent to Bot2 via Telethon
4. User sees a live timer updating every 30 seconds
5. Bot2 processes the APK and returns the result
6. Bot downloads the processed APK and delivers it to the user automatically
7. Timer message is deleted, processed APK sent as reply to original message

### Manual admin flow (fallback)

1. Admin receives the forwarded APK in their chat
2. Admin processes it externally and **replies** to the forwarded message with the result
3. Bot delivers it to the user and confirms in the admin chat

### Channel broadcast flow

1. Admin posts anything in the configured channel
2. Bot detects the post and forwards it to every registered user
3. Users who blocked the bot are skipped silently

---

## Admin Commands

| Command | Description | Example |
|---|---|---|
| `/listusers` | List all registered users | `/listusers` |
| `/userinfo <id>` | View full details of a user | `/userinfo 45231` |
| `/setpaid <id> yes\|no` | Mark user as paid or unpaid | `/setpaid 45231 yes` |
| `/settokens <id> <limit>` | Set daily APK token limit | `/settokens 45231 5` |
| `/setexpiry <id> YYYY-MM-DD` | Set subscription expiry (max 30 days) | `/setexpiry 45231 2025-05-28` |
| `/broadcast <text>` | Broadcast text to all users | `/broadcast Update is live!` |
| `/broadcast` (reply) | Forward any message to all users | *(reply to a message with /broadcast)* |

> `/broadcast` also works by replying to any existing message — that message gets forwarded to all users.

## User Commands

| Command | Description |
|---|---|
| `/start` | Show welcome message with account info |
| `/info` | Check your User ID, tokens, and subscription status |

---

## Configuration — `.env`

```env
# ── Required ──────────────────────────────────────────────
TELEGRAM_BOT_TOKEN=your_bot_token_here
ADMIN_TELEGRAM_ID=your_telegram_id_here

# ── Bot2 automated relay (leave blank to disable) ─────────
API_ID=your_telegram_api_id
API_HASH=your_telegram_api_hash
PHONE_NUMBER=+1234567890
BOT2_USERNAME=@the_processing_bot
# Auto-saved after first login — skip OTP on restart
TELETHON_SESSION=

# ── Channel broadcast (leave blank to disable) ────────────
# Use @username or numeric ID (e.g. -1001234567890)
CHANNEL_ID=@yourchannel
```

### Where to get each value

| Variable | Where to get it |
|---|---|
| `TELEGRAM_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) → create bot |
| `ADMIN_TELEGRAM_ID` | [@userinfobot](https://t.me/userinfobot) |
| `API_ID` / `API_HASH` | [my.telegram.org](https://my.telegram.org) → API development tools |
| `PHONE_NUMBER` | Your personal Telegram account number |
| `BOT2_USERNAME` | Username of the secondary processing bot |
| `CHANNEL_ID` | Your channel's `@username` or numeric ID |

> To add **multiple admins** separate IDs with a comma:
> ```
> ADMIN_TELEGRAM_ID=111111111,222222222
> ```

---

## Project Structure

```
eve-main/
├── bot.py                   # Main bot code (all logic)
├── .env                     # Your credentials (never commit this)
├── .env.example             # Template for .env
├── requirements.txt         # Python dependencies
├── render.yaml              # Render.com deployment config
├── telegram-bot.service     # Linux systemd service file
├── users.json               # Auto-created: user data
├── deliveries.json          # Auto-created: pending deliveries
└── submission_counter.json  # Auto-created: submission ID counter
```

### Data files (auto-created on first run)

| File | Purpose |
|---|---|
| `users.json` | All user records: paid status, tokens, expiry |
| `deliveries.json` | Tracks pending APK deliveries from admin to user |
| `submission_counter.json` | Sequential submission ID counter |

> **Do not delete these files** while the bot is running. Back them up regularly.

---

## Requirements

- Python **3.12** (recommended — do not use 3.13, 3.14, or 3.15)
- A Telegram Bot Token (from [@BotFather](https://t.me/BotFather))
- Your personal Telegram user ID (from [@userinfobot](https://t.me/userinfobot))
- *(Optional)* Telegram API credentials for Bot2 relay ([my.telegram.org](https://my.telegram.org))

---

## Deployment — DigitalOcean Windows Server 2022 RDP (with PM2)

### Step 1 — Create the Droplet

1. Log in to [digitalocean.com](https://digitalocean.com)
2. Click **Create → Droplets**
3. Choose:
   - **Image:** `Windows Server 2022 Standard`
   - **Size:** Basic → **2 GB RAM / 1 CPU** ($28/mo minimum for Windows)
   - **Authentication:** Password → set a strong password
4. Click **Create Droplet** and wait ~3 minutes
5. Copy the **IP address** from your dashboard

---

### Step 2 — Connect via RDP

1. On your PC press `Win + R` → type `mstsc` → Enter
2. **Computer:** paste the droplet IP address
3. **Username:** `Administrator`
4. **Password:** the one you set above
5. Click **OK** → accept the certificate warning

---

### Step 3 — Install Python 3.12

> ⚠️ Do NOT install Python 3.13, 3.14, or 3.15 — they break python-telegram-bot.

1. Open **Edge** on the server and go to:
   ```
   https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe
   ```
2. Run the installer
3. ✅ Tick **"Add Python to PATH"**
4. Click **Install Now**
5. Verify in Command Prompt:
   ```cmd
   python --version
   ```
   Must show `Python 3.12.x`

---

### Step 4 — Install Git

1. Open Edge and go to: `https://git-scm.com/download/win`
2. Download and run the installer — click **Next** all the way through
3. Verify:
   ```cmd
   git --version
   ```

---

### Step 5 — Install Node.js (required for PM2)

1. Open Edge and go to:
   ```
   https://nodejs.org/dist/v22.13.1/node-v22.13.1-x64.msi
   ```
2. Run installer → click **Next** all the way → **Finish**
3. Verify:
   ```cmd
   node --version
   npm --version
   ```

---

### Step 6 — Install PM2

```cmd
npm install -g pm2
npm install -g pm2-windows-startup
```

---

### Step 7 — Clone the Project

```cmd
cd C:\
git clone https://github.com/redteamhere/autobot.git eve-main
cd C:\eve-main
```

---

### Step 8 — Create Virtual Environment and Install Dependencies

```cmd
cd C:\eve-main
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

---

### Step 9 — Create the .env File

```cmd
notepad C:\eve-main\.env
```

Paste and fill in your values:

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
ADMIN_TELEGRAM_ID=your_telegram_id_here

# Bot2 relay (optional — leave blank to disable)
API_ID=
API_HASH=
PHONE_NUMBER=
BOT2_USERNAME=
TELETHON_SESSION=

# Channel broadcast (optional — leave blank to disable)
CHANNEL_ID=
```

Press `Ctrl + S` → close Notepad.

> **First run with Bot2:** the bot will ask for an OTP on your phone number. Enter it once — the session is auto-saved to `.env` and never asked again.

---

### Step 10 — Channel Broadcast Setup (optional)

1. Set `CHANNEL_ID=@yourchannel` in `.env`
2. Open your channel in Telegram → **Edit → Administrators → Add Administrator**
3. Search for your bot and add it — it only needs **"Post Messages"** permission
4. Now any post you make in the channel will be forwarded to all registered users

---

### Step 11 — Test Run (optional but recommended)

```cmd
cd C:\eve-main
venv\Scripts\activate
python bot.py
```

You should see:
```
Telethon client started — bot2: @android_protect_bot
Application started
```

Test in Telegram, then stop with `Ctrl + C`.

---

### Step 12 — Start Bot with PM2 (24/7)

> ⚠️ Always use the **full path** for the interpreter — relative paths will fail.

```cmd
pm2 start C:\eve-main\bot.py --name "telegram-bot" --interpreter C:\eve-main\venv\Scripts\python.exe
```

---

### Step 13 — Auto-start on Server Reboot

```cmd
pm2 save
pm2-startup install
```

---

### Step 14 — Verify It Is Running

```cmd
pm2 status
```

You should see:

```
┌────┬─────────────────┬───────┬────────┬─────────┐
│ id │ name            │ mode  │ status │ restart │
├────┼─────────────────┼───────┼────────┼─────────┤
│ 0  │ telegram-bot    │ fork  │ online │ 0       │
└────┴─────────────────┴───────┴────────┴─────────┘
```

**You can now close RDP — the bot stays running 24/7.** ✅

---

### PM2 Management Commands

```cmd
# Check status
pm2 status

# View live logs
pm2 logs telegram-bot

# Restart bot
pm2 restart telegram-bot

# Stop bot
pm2 stop telegram-bot

# Delete from PM2
pm2 delete telegram-bot
```

---

### Updating the Bot After Code Changes

```cmd
cd C:\eve-main
git pull origin main
pm2 restart telegram-bot
```

---

## Deployment — Railway

Railway is a simple cloud platform that runs the bot 24/7. It supports persistent volumes so your user data survives redeploys.

### Step 1 — Push to GitHub

Make sure your code is pushed to GitHub (already done if you followed earlier steps).

### Step 2 — Create a Railway project

1. Go to [railway.app](https://railway.app) and sign in with GitHub
2. Click **New Project → Deploy from GitHub repo**
3. Select your `autobot` repository
4. Railway will auto-detect Python and start building

### Step 3 — Set environment variables

Go to your project → **Variables** tab → add each of these:

| Variable | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your bot token |
| `ADMIN_TELEGRAM_ID` | Your Telegram user ID |
| `API_ID` | Telegram API ID (account 1) |
| `API_HASH` | Telegram API hash (account 1) |
| `PHONE_NUMBER` | Phone number (account 1) |
| `BOT2_USERNAME` | `@android_protect_bot` |
| `TELETHON_SESSION` | Your session string |
| `API_ID_2` | *(optional)* Account 2 API ID |
| `API_HASH_2` | *(optional)* Account 2 API hash |
| `PHONE_NUMBER_2` | *(optional)* Account 2 phone |
| `TELETHON_SESSION_2` | *(optional)* Account 2 session |
| `CHANNEL_ID` | *(optional)* `@yourchannel` |

> **TELETHON_SESSION** — copy the full session string from your local `.env` file. This skips OTP on Railway.

### Step 4 — Add a Volume (persistent data)

Without a volume, your `users.json`, `deliveries.json`, and `submission_counter.json` are **wiped on every redeploy**.

1. In your Railway project click **New → Volume**
2. Set **Mount Path** to `/app`
3. Click **Create**

Railway will now persist all data files between deploys and restarts.

### Step 5 — Deploy

Railway deploys automatically when you push to GitHub. To trigger a manual deploy:
- Go to your project → **Deployments** → click **Deploy**

### Step 6 — Check logs

Click your service → **Logs** tab. You should see:
```
Telethon client started (account 1) — bot2: @android_protect_bot
Admin commands set for ...
Application started
```

**Bot is live. ✅**

### Updating after code changes

Just push to GitHub — Railway auto-redeploys:
```cmd
git add .
git commit -m "update"
git push
```

---

## Deployment — Render.com (free cloud hosting)

1. Push the project to a GitHub repository
2. Go to [render.com](https://render.com) → **New → Background Worker**
3. Connect your GitHub repo
4. Set environment variables in Render's dashboard:
   - `TELEGRAM_BOT_TOKEN`
   - `ADMIN_TELEGRAM_ID`
   - `API_ID`, `API_HASH`, `PHONE_NUMBER`, `BOT2_USERNAME` *(optional)*
   - `CHANNEL_ID` *(optional)*
5. Render will auto-deploy using `render.yaml`

---

## Deployment — Linux Server (systemd)

Use the included `telegram-bot.service` file.

### 1. Edit the service file

Replace the placeholders:
```
User=__USER__              →  your Linux username (e.g. ubuntu)
WorkingDirectory=__HOME__/eve-main
ExecStart=__HOME__/eve-main/venv/bin/python bot.py
```

### 2. Install and enable

```bash
sudo cp telegram-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable telegram-bot
sudo systemctl start telegram-bot
```

### 3. Useful commands

```bash
sudo systemctl status telegram-bot     # check status
sudo journalctl -u telegram-bot -f     # live logs
sudo systemctl restart telegram-bot    # restart
sudo systemctl stop telegram-bot       # stop
```

---

## Quick Start Summary

```cmd
# 1. Clone
git clone https://github.com/redteamhere/autobot.git eve-main
cd C:\eve-main

# 2. Setup
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt

# 3. Configure
notepad .env

# 4. Run with PM2
pm2 start C:\eve-main\bot.py --name "telegram-bot" --interpreter C:\eve-main\venv\Scripts\python.exe
pm2 save
pm2-startup install
```
