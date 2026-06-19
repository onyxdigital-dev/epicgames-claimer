import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .database import init_db, get_claimed_games, get_setting, set_setting
from .scheduler import start_scheduler, stop_scheduler, scheduler
from .state import state

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="/app/app/templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(lifespan=lifespan)


def _next_run_str() -> str:
    job = scheduler.get_job("weekly_claim")
    if not job:
        return "Not scheduled"
    next_run = job.next_run_time
    if not next_run:
        return "Unknown"
    delta = next_run - datetime.now(next_run.tzinfo)
    days = delta.days
    hours, rem = divmod(delta.seconds, 3600)
    minutes = rem // 60
    if days > 0:
        return f"in {days}d {hours}h {minutes}m"
    if hours > 0:
        return f"in {hours}h {minutes}m"
    return f"in {minutes}m"


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    games = await get_claimed_games()
    twofa_seconds_left = None
    if state.waiting_for_2fa and state.twofa_deadline:
        twofa_seconds_left = max(0, int(state.twofa_deadline - time.time()))

    return templates.TemplateResponse("home.html", {
        "request": request,
        "games": games,
        "next_run": _next_run_str(),
        "waiting_for_2fa": state.waiting_for_2fa,
        "twofa_seconds_left": twofa_seconds_left,
        "last_run_status": state.last_run_status,
        "last_run_time": state.last_run_time,
    })


@app.post("/submit-2fa")
async def submit_2fa(code: str = Form(...)):
    if state.waiting_for_2fa and state.twofa_future and not state.twofa_future.done():
        state.twofa_future.get_loop().call_soon_threadsafe(state.twofa_future.set_result, code.strip())
    return RedirectResponse("/", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "epic_email": await get_setting("epic_email") or "",
        "notify_url": await get_setting("notify_url") or "",
        "notify_type": await get_setting("notify_type") or "ntfy",
        "saved": request.query_params.get("saved"),
    })


@app.post("/settings")
async def save_settings(
    epic_email: str = Form(""),
    epic_password: str = Form(""),
    notify_url: str = Form(""),
    notify_type: str = Form("ntfy"),
):
    if epic_email:
        await set_setting("epic_email", epic_email)
    if epic_password:
        await set_setting("epic_password", epic_password)
    await set_setting("notify_url", notify_url)
    await set_setting("notify_type", notify_type)
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/trigger")
async def trigger_claim():
    from .claimer import run_claim_job
    asyncio.create_task(run_claim_job())
    return RedirectResponse("/", status_code=303)
