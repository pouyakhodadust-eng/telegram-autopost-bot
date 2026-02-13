# Telegram Autopost Bot

A Python Telegram bot (aiogram v3) that:
- Detects when it is added to groups
- Sends an immediate message with contact links
- Repeats the message every 24 hours per group
- Persists schedules across restarts (SQLite/PostgreSQL)
- Supports admin commands to enable/disable autopost

## Architecture Choices

### Framework: aiogram v3
- Modern async API
- Native `my_chat_member` handling for group join/leave
- Active maintenance and good documentation

### Default: autopost enabled when added
When the bot is added to a group, autopost is **enabled by default**. Rationale:
- Users add the bot specifically for this messaging purpose
- Disabling is one command (`/disable_autopost`) if unwanted
- Safer alternative would be disabled-by-default; change in `bot.py` if preferred

### Scheduling: custom async loop vs APScheduler
A **custom async loop** polls the DB every 60 seconds for due chats. Benefits:
- All state lives in our DB; no external job store
- Survives restarts without duplicate jobs
- Simpler than APScheduler + SQLAlchemy job store (which uses sync engine)
- Catch-up: if offline past schedule, sends one message then next in 24h

### Database: SQLAlchemy async
- SQLite by default (no setup)
- PostgreSQL via `DATABASE_URL` for production
- Single connection string switch

## Project Layout

```
.
├── bot.py          # Main entry, handlers, commands
├── config.py       # Configuration from env
├── db.py           # Database layer (chats table)
├── scheduler.py    # 24h repeat logic
├── requirements.txt
├── .env.example
├── data/           # SQLite DB (created automatically)
└── deploy/
    └── telegram-autopost.service
```

## Setup

### 1. Create a bot
1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Use `/newbot` and follow prompts
3. Copy the token

### 2. Local setup

```bash
# Clone or copy project
cd telegram-autopost

# Create virtual environment
python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env and set BOT_TOKEN=your_token_here
```

### 3. Run locally

```bash
python bot.py
```

## Deployment (VPS with systemd)

### 1. Prepare the server
```bash
# On your VPS (Ubuntu/Debian)
sudo apt update
sudo apt install python3 python3-venv python3-pip -y
```

### 2. Deploy the bot
```bash
# Create app user (optional but recommended)
sudo useradd -r -s /bin/false telegram-bot

# Copy project to /opt/telegram-autopost (or your path)
sudo mkdir -p /opt/telegram-autopost
sudo cp -r bot.py config.py db.py scheduler.py requirements.txt /opt/telegram-autopost/
sudo cp -r deploy /opt/telegram-autopost/

# Set ownership
sudo chown -R telegram-bot:telegram-bot /opt/telegram-autopost
```

### 3. Install Python dependencies
```bash
cd /opt/telegram-autopost
sudo -u telegram-bot python3 -m venv venv
sudo -u telegram-bot venv/bin/pip install -r requirements.txt
```

### 4. Configure environment
```bash
sudo -u telegram-bot cp .env.example .env
sudo -u telegram-bot nano .env   # Set BOT_TOKEN
```

### 5. systemd service
Edit `deploy/telegram-autopost.service`:

```ini
[Unit]
Description=Telegram Autopost Bot
After=network.target

[Service]
Type=simple
User=telegram-bot
WorkingDirectory=/opt/telegram-autopost
ExecStart=/opt/telegram-autopost/venv/bin/python bot.py
Restart=always
RestartSec=10
EnvironmentFile=/opt/telegram-autopost/.env

[Install]
WantedBy=multi-user.target
```

Install and run:
```bash
sudo cp deploy/telegram-autopost.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable telegram-autopost
sudo systemctl start telegram-autopost
sudo systemctl status telegram-autopost
```

### 6. Logs
```bash
sudo journalctl -u telegram-autopost -f
```

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `BOT_TOKEN` | Yes | - | Bot token from BotFather |
| `DATABASE_URL` | No | `sqlite:///./data/bot.db` | SQLite or PostgreSQL URL |
| `MIN_SEND_INTERVAL_SECONDS` | No | 2.0 | Min seconds between sends (rate limit) |
| `LOG_LEVEL` | No | INFO | DEBUG, INFO, WARNING, ERROR |

### PostgreSQL
```bash
# Install driver
pip install asyncpg

# Set DATABASE_URL
DATABASE_URL=postgresql://user:password@localhost:5432/botdb
```

## Commands (group admins only)

- `/enable_autopost` — Enable daily posting
- `/disable_autopost` — Disable daily posting  
- `/status` — Show enabled, last_sent_at, next_send_at

## Testing / Verification Checklist

- [ ] Add bot to group → immediate message sent
- [ ] Wait or simulate 24h → message repeats (change `POLL_INTERVAL` in scheduler.py to 10 seconds for quick tests, and `DEFAULT_INTERVAL_HOURS` to 0.001 for ~3.6s)
- [ ] Restart bot → schedule continues (DB persisted)
- [ ] Remove bot from group → autopost stops (chat marked disabled)
- [ ] `/disable_autopost` → stops sending
- [ ] `/enable_autopost` → resumes
- [ ] `/status` → shows correct last/next times
