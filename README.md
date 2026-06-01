# OTP Service Telegram Bot

This project is a refactored OTP service bot for Telegram built with python-telegram-bot (v20+), aiosqlite and dotenv.

Requirements
- Python 3.10+
- See `requirements.txt`

Setup
1. Copy `.env.example` to `.env` and fill in your `BOT_TOKEN` and other values.
2. Install dependencies:

```bash
python -m pip install -r requirements.txt
```

3. Run the bot:

```bash
python bot.py
```

Admin API
----------
The repository includes a small FastAPI admin scaffold in `admin_api.py`. Run it with:

```bash
uvicorn admin_api:app --host 0.0.0.0 --port 8000
```

Set `ADMIN_API_TOKEN` in your `.env` and include it as `X-Admin-Token` header for requests.

Notes & testing
- I ran a static syntax check locally in the workspace; live Telegram flows (sending messages, callbacks) require a valid `BOT_TOKEN` and actual Telegram users to test.
- Admin commands are protected by `OWNER_ID` and `SUPPORT_ADMINS`.

What I changed
- Rewrote the bot as `bot.py` with a clean command and callback structure.
- Added DB migrations on startup and safer transactional handling for money and SIM locking.
- Added `requirements.txt` and `.env.example`.

Notes & testing
- I ran a static syntax check locally in the workspace; live Telegram flows (sending messages, callbacks) require a valid `BOT_TOKEN` and actual Telegram users to test.
- Admin commands are protected by `OWNER_ID` and `SUPPORT_ADMINS`.

Security & compliance
- Terms are included in bot help and in messages: use only numbers/accounts you are authorized to manage.
- Do not use this bot to access accounts you do not own.

If you want, I can:
- Add a `Dockerfile` and `systemd` unit
- Add unit tests for DB helpers
- Harden rate limiting
