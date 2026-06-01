import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException
import aiosqlite
from dotenv import load_dotenv
from prometheus_client import Counter, generate_latest, CONTENT_TYPE_LATEST
from fastapi.responses import Response

load_dotenv()
ADMIN_API_TOKEN = os.getenv("ADMIN_API_TOKEN", "changeme")
DB_FILE = os.getenv("DB_FILE", "otp_bot.db")

app = FastAPI(title="OTP Bot Admin API")

# basic metrics
requests_counter = Counter("admin_api_requests_total", "Total admin API requests")

@asynccontextmanager
async def db_connect():
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        yield db

async def require_token(x_admin_token: str | None = Header(None)):
    if x_admin_token != ADMIN_API_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/stats")
async def stats(x_admin_token: str | None = Header(None)):
    await require_token(x_admin_token)
    requests_counter.inc()
    async with db_connect() as db:
        users = await (await db.execute("SELECT COUNT(*) AS v FROM users")).fetchone()
        active_sims = await (await db.execute("SELECT COUNT(*) AS v FROM sim_numbers WHERE status = 'active'")).fetchone()
        pending = await (await db.execute("SELECT COUNT(*) AS v FROM recharges WHERE status = 'pending'")).fetchone()
        waiting = await (await db.execute("SELECT COUNT(*) AS v FROM otp_requests WHERE status = 'waiting'")).fetchone()
    return {
        "users": users['v'],
        "active_sims": active_sims['v'],
        "pending_recharges": pending['v'],
        "waiting_otps": waiting['v'],
    }


@app.get("/recharges/pending")
async def pending_recharges(x_admin_token: str | None = Header(None)):
    await require_token(x_admin_token)
    async with db_connect() as db:
        rows = await (await db.execute("SELECT ref, user_id, amount, created_at FROM recharges WHERE status = 'pending' ORDER BY created_at DESC LIMIT 100")).fetchall()
    return [dict(r) for r in rows]


@app.get("/disputes")
async def disputes(x_admin_token: str | None = Header(None)):
    await require_token(x_admin_token)
    async with db_connect() as db:
        rows = await (await db.execute("SELECT request_id, taker_id, giver_id, sim_id, created_at FROM otp_requests WHERE status = 'disputed' ORDER BY created_at DESC LIMIT 100")).fetchall()
    return [dict(r) for r in rows]
