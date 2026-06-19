import asyncio


class AppState:
    def __init__(self):
        self.waiting_for_2fa: bool = False
        self.twofa_future: asyncio.Future | None = None
        self.twofa_deadline: float | None = None  # time.time() value when code expires
        self.last_run_status: str = "never"  # "never" | "success" | "failed" | "running"
        self.last_run_time: str | None = None


state = AppState()
