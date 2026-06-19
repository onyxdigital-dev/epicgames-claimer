from datetime import datetime, timezone


class AppState:
    def __init__(self):
        self.last_run_status: str = "never"  # "never" | "success" | "failed" | "running"
        self.last_run_time: str | None = None
        self.pending_checkout_url: str = ""
        self.pending_game_titles: list[str] = []


state = AppState()
