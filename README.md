# Telegram APK Protection Bot

A Telegram bot that lets users submit APK files for protection. Files are forwarded to the admin, and the admin manually replies with the processed APK which is then delivered back to the user with a live waiting timer.

---

## Features

- Users send an APK → bot forwards it to admin immediately
- Live timer message updates every 30 seconds while the user waits
- Admin replies to the forwarded message with the processed APK → bot delivers it to the user automatically and deletes the waiting message
- Delivered APK sent as a reply to the user's original message (clear context)
- Paid / trial user system with daily and monthly token limits
- Max APK size: **20 MB**
- Max subscription: **30 days**
- Monthly cap: **500 APKs per user**
- Unique random submission IDs (always increasing, starting from 1000)
- Admin-only backend commands (invisible to regular users)
- Multiple concurrent users supported

---

## Requirements

- Python **3.12** (recommended — do not use 3.13, 3.14, or 3.15)
- A Telegram Bot Token (from [@BotFather](https://t.me/BotFather))
- Your personal Telegram user ID (from [@userinfobot](https://t.me/userinfobot))

---

## Project Structure

```
telegram_bot/
├── bot.py                   # Main bot code
├── .env                     # Your secret tokens (never commit this)
├── .env.example             # Template for .env
├── requirements.txt         # Python dependencies
├── render.yaml              # Render.com deployment config
├── telegram-bot.service     # Linux systemd service file
├── users.json               # Auto-created: user data
├── deliveries.json          # Auto-created: pending deliveries
└── submission_counter.json  # Auto-created: submission ID counter
```

---

## Data Files

These files are created automatically on first run:

| File | Purpose |
|---|---|
| `users.json` | Stores all user records (paid status, tokens, expiry) |
| `deliveries.json` | Tracks pending APK deliveries from admin to user |
| `submission_counter.json` | Tracks the submission ID counter |

> **Do not delete these files** while the bot is running. Back them up regularly.

---

## How It Works

### User flow

1. User sends `/start` → sees their User ID, paid status, and token balance
2. User sends an APK file (max 20 MB)
3. Bot checks: size limit → rate limit (10 s cooldown) → token balance
4. If allowed: APK is forwarded to admin, user sees a live timer message
5. Timer updates every 30 seconds showing elapsed time
6. When admin delivers the processed APK, the timer message is deleted and the user receives the protected APK as a reply to their original file

### Admin flow

1. Admin receives the forwarded APK in their Telegram chat
2. Admin processes the APK externally
3. Admin **replies** to the forwarded message with the processed APK file
4. Bot automatically:
   - Deletes the user's waiting/timer message
   - Sends the processed APK to the user as a reply to their original message
   - Confirms delivery in the admin chat

---

## Admin Commands

These commands are only visible and accessible to admins:

| Command | Description | Example |
|---|---|---|
| `/listusers` | List all users with status | `/listusers` |
| `/userinfo <id>` | View full details of a user | `/userinfo 45231` |
| `/setpaid <id> yes\|no` | Mark user as paid or unpaid | `/setpaid 45231 yes` |
| `/settokens <id> <limit>` | Set daily APK token limit | `/settokens 45231 5` |
| `/setexpiry <id> YYYY-MM-DD` | Set subscription expiry date (max 30 days) | `/setexpiry 45231 2025-05-28` |

> You must set a user as paid (`/setpaid`) before setting tokens or expiry.

## User Commands

| Command | Description |
|---|---|
| `/start` | Show welcome message with account info |
| `/info` | Check your User ID, tokens, and subscription status |

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
git clone https://github.com/redteamhere/eve.git telegram_bot
cd C:\telegram_bot
```

---

### Step 8 — Create Virtual Environment and Install Dependencies

```cmd
cd C:\telegram_bot
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

---

### Step 9 — Create the .env File

```cmd
notepad C:\telegram_bot\.env
```

Paste this into Notepad:

```
TELEGRAM_BOT_TOKEN=your_bot_token_here
ADMIN_TELEGRAM_ID=your_telegram_id_here
```

- Get `TELEGRAM_BOT_TOKEN` from [@BotFather](https://t.me/BotFather)
- Get `ADMIN_TELEGRAM_ID` from [@userinfobot](https://t.me/userinfobot)

Press `Ctrl + S` → close Notepad.

> To add multiple admins separate IDs with a comma:
> ```
> ADMIN_TELEGRAM_ID=111111111,222222222
> ```

---

### Step 10 — Test Run (optional but recommended)

```cmd
cd C:\telegram_bot
venv\Scripts\activate
python bot.py
```

You should see `Application started`. Test in Telegram, then stop with `Ctrl + C`.

---

### Step 11 — Start Bot with PM2 (24/7)

> ⚠️ Always use the **full path** for the interpreter — relative paths will fail.

```cmd
pm2 start C:\telegram_bot\bot.py --name "telegram-bot" --interpreter C:\telegram_bot\venv\Scripts\python.exe
```

---

### Step 12 — Auto-start on Server Reboot

```cmd
pm2 save
pm2-startup install
```

---

### Step 13 — Verify It Is Running

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

When you push new code to GitHub and want to update the server:

```cmd
cd C:\telegram_bot
git pull origin main
pm2 restart telegram-bot
```

---

## Deployment — Render.com (free cloud hosting)

1. Push the project to a GitHub repository
2. Go to [render.com](https://render.com) → **New → Background Worker**
3. Connect your GitHub repo
4. Set environment variables in Render's dashboard:
   - `TELEGRAM_BOT_TOKEN` = your bot token
   - `ADMIN_TELEGRAM_ID` = your telegram ID
5. Render will auto-deploy using `render.yaml`

---

## Deployment — Linux Server (always on with systemd)

Use the included `telegram-bot.service` file.

### 1. Edit the service file

Replace the placeholders:
```
User=__USER__        →  your Linux username  (e.g. ubuntu)
WorkingDirectory=__HOME__/telegram_bot  →  e.g. /home/ubuntu/telegram_bot
ExecStart=__HOME__/telegram_bot/venv/bin/python bot.py
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
git clone https://github.com/redteamhere/eve.git telegram_bot
cd telegram_bot

# 2. Setup
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt

# 3. Configure
notepad .env

# 4. Run with PM2
pm2 start C:\telegram_bot\bot.py --name "telegram-bot" --interpreter C:\telegram_bot\venv\Scripts\python.exe
pm2 save
pm2-startup install
```
