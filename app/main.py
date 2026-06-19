import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .claimer import connect_with_auth_code, is_connected, run_claim_job, AUTH_CODE_URL
from .database import init_db, get_claimed_games, get_setting, set_setting
from .logbuffer import get_logs, install as install_log_buffer
from .scheduler import start_scheduler, stop_scheduler, scheduler
from .state import state

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
install_log_buffer()
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
    delta = next_run - __import__("datetime").datetime.now(next_run.tzinfo)
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
    connected = await is_connected()
    return templates.TemplateResponse("home.html", {
        "request": request,
        "games": games,
        "next_run": _next_run_str(),
        "connected": connected,
        "auth_code_url": AUTH_CODE_URL,
        "last_run_status": state.last_run_status,
        "last_run_time": state.last_run_time,
        "pending_checkout_url": state.pending_checkout_url,
        "pending_game_titles": state.pending_game_titles,
    })


@app.post("/connect")
async def connect(request: Request, auth_code: str = Form(...)):
    error = None
    try:
        await connect_with_auth_code(auth_code.strip())
    except RuntimeError as e:
        error = str(e)

    if error:
        games = await get_claimed_games()
        return templates.TemplateResponse("home.html", {
            "request": request,
            "games": games,
            "next_run": _next_run_str(),
            "connected": False,
            "auth_code_url": AUTH_CODE_URL,
            "last_run_status": state.last_run_status,
            "last_run_time": state.last_run_time,
            "connect_error": error,
        }, status_code=400)

    return RedirectResponse("/", status_code=303)


@app.post("/disconnect")
async def disconnect():
    for key in ("device_account_id", "device_id", "device_secret"):
        await set_setting(key, "")
    return RedirectResponse("/", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "notify_url": await get_setting("notify_url") or "",
        "notify_type": await get_setting("notify_type") or "ntfy",
        "capsolver_key": await get_setting("capsolver_key") or "",
        "saved": request.query_params.get("saved"),
    })


@app.post("/settings")
async def save_settings(
    notify_url: str = Form(""),
    notify_type: str = Form("ntfy"),
    capsolver_key: str = Form(""),
):
    await set_setting("notify_url", notify_url)
    await set_setting("notify_type", notify_type)
    await set_setting("capsolver_key", capsolver_key.strip())
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/trigger")
async def trigger_claim():
    asyncio.create_task(run_claim_job())
    return RedirectResponse("/", status_code=303)


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    if request.query_params.get("partial"):
        return JSONResponse(get_logs())
    return templates.TemplateResponse("logs.html", {
        "request": request,
        "logs": get_logs(),
    })
