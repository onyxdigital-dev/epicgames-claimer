# Epic Games Auto-Claimer — Design Spec

**Date:** 2026-06-19  
**Status:** Approved

---

## Overview

A self-hosted Docker container for Unraid that automatically claims free Epic Games games every Thursday at 11 AM ET. Provides a web dashboard to view claim history and submit 2FA codes when prompted. Built from scratch in Python.

---

## Architecture

Single Python process with three logical layers sharing a SQLite database and an in-memory state object:

| Layer | Technology | Responsibility |
|---|---|---|
| Scheduler | APScheduler | Fires claim job every Thursday 11 AM ET |
| Claim job | httpx | Epic Games API login, free game detection, claiming |
| Web dashboard | FastAPI + Jinja2 | UI for history, 2FA prompt, settings |

No inter-process communication needed — all layers run in the same process and share state directly.

---

## Data Flow

```
Thursday 11 AM ET
    → APScheduler fires claim job
    → POST to Epic login endpoint (email + password)
    → Epic responds: 2FA required
    → Claim job sets in-memory flag: waiting_for_2fa = True
    → Optional: POST to notification webhook (ntfy / Gotify / Pushover)
    → User opens dashboard on port 3000
    → Dashboard shows 2FA form (only visible when flag is set)
    → User submits 2FA code → POST /submit-2fa
    → Claim job resumes with code → completes login
    → Fetches current free games from Epic catalog endpoint
    → Claims each free game via Epic order endpoint
    → Writes each claimed game to SQLite (title, date, cover art URL)
    → Flag cleared, dashboard updates to show new claims
```

If no 2FA is required (Epic sometimes skips it for trusted sessions), the job proceeds directly to claiming without waiting.

---

## Epic Games API Interactions

All interactions are direct HTTP calls — no browser automation. Endpoints used:

- **Login:** `POST https://account-public-service-prod.ol.epicgames.com/account/api/oauth/token`
- **2FA:** `POST https://account-public-service-prod.ol.epicgames.com/account/api/oauth/token` (with mfa_token)
- **Free games catalog:** `GET https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions`
- **Claim order:** `POST https://payment-website-pci.ol.epicgames.com/purchase` (zero-cost order)

Session tokens are stored in SQLite and reused across runs to reduce 2FA prompts where possible.

---

## Web Dashboard

Three views served by FastAPI + Jinja2 templates:

### Home (`/`)
- List of claimed games: cover art thumbnail, title, date claimed
- Next scheduled run countdown
- Manual "Claim Now" trigger button

### 2FA Prompt (`/`)
- Overlaid on Home when `waiting_for_2fa = True`
- Form with single text input for the 2FA code
- Auto-dismisses after successful submission
- Shows timeout countdown (code expires after 10 minutes)

### Settings (`/settings`)
- Epic email (masked display, editable)
- Epic password (write-only)
- Notification webhook URL (optional)
- Notification webhook type: `ntfy` | `gotify` | `pushover`
- Save button writes to `config/settings.db` (SQLite)

---

## Database Schema

**`claimed_games` table**
```sql
CREATE TABLE claimed_games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    claimed_at DATETIME NOT NULL,
    cover_url TEXT,
    epic_id TEXT UNIQUE
);
```

**`settings` table**
```sql
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
```

Stored keys: `epic_email`, `epic_password`, `notify_url`, `notify_type`, `session_token`, `session_expiry`.

---

## Configuration (Environment Variables)

| Variable | Required | Default | Description |
|---|---|---|---|
| `EPIC_EMAIL` | Yes* | — | Epic Games account email |
| `EPIC_PASSWORD` | Yes* | — | Epic Games account password |
| `NOTIFY_WEBHOOK_URL` | No | — | Webhook URL for 2FA notifications |
| `NOTIFY_WEBHOOK_TYPE` | No | — | `ntfy`, `gotify`, or `pushover` |
| `TZ` | No | `America/New_York` | Container timezone |

*Can also be set via the Settings page in the dashboard, stored in SQLite.

---

## Notification Payload

When 2FA is required, a POST is sent to the configured webhook URL:

- **ntfy:** `POST <url>` with body `"Epic Games needs your 2FA code: http://<host>:3000"`
- **Gotify:** `POST <url>/message` with JSON `{"title": "Epic Claimer", "message": "..."}`
- **Pushover:** `POST https://api.pushover.net/1/messages.json` with standard payload

---

## Docker Setup

### Image
- Base: `python:3.12-alpine`
- Estimated size: ~120MB
- Exposed port: `3000`
- Volume: `/config` (SQLite database)

### docker-compose.yml
```yaml
services:
  epicgames-claimer:
    image: epicgames-claimer:latest
    container_name: epicgames-claimer
    ports:
      - "3000:3000"
    volumes:
      - ./config:/config
    environment:
      - EPIC_EMAIL=your@email.com
      - EPIC_PASSWORD=yourpassword
      - NOTIFY_WEBHOOK_URL=http://your-ntfy-server/epicgames
      - NOTIFY_WEBHOOK_TYPE=ntfy
      - TZ=America/New_York
    restart: unless-stopped
```

### Unraid Template
An Unraid Community Applications XML template will be included at `unraid/epicgames-claimer.xml` with pre-filled port mappings, volume paths, and environment variable descriptions.

---

## Project Structure

```
epicgames-claimer/
├── Dockerfile
├── docker-compose.yml
├── unraid/
│   └── epicgames-claimer.xml
├── app/
│   ├── main.py          # FastAPI app + APScheduler startup
│   ├── claimer.py       # Epic Games API login + claim logic
│   ├── scheduler.py     # APScheduler job definition
│   ├── notify.py        # Webhook notification dispatch
│   ├── database.py      # SQLite setup + queries
│   ├── state.py         # In-memory state (waiting_for_2fa flag)
│   └── templates/
│       ├── base.html
│       ├── home.html
│       └── settings.html
└── requirements.txt
```

---

## Error Handling

- **Login failure** (wrong credentials): Log error, send notification, do not retry until next scheduled run.
- **2FA timeout** (user doesn't submit within 10 minutes): Clear waiting state, log failure, send notification.
- **Game already owned**: Skip silently (Epic API returns error code `errors.com.epicgames.purchase.purchase.already_owned`).
- **No free games this week**: Log info, do nothing.
- **Network error**: Retry up to 3 times with 30-second backoff, then log failure and notify.

---

## Out of Scope

- TOTP/authenticator-based 2FA (to avoid requiring users to change their 2FA setup)
- Browser automation / Playwright
- Multi-account support
- Game library sync beyond claimed-through-this-tool history
