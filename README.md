# praison

*praise + prison* — a self-hosted web app for planning your hours on a Praise time-tracking instance.

Successor to recorules (the same idea, for Recoru). Brings back the two features Praise lacks:

- **Plan future days** (office / WFH / paid leave per day) with live monthly projection
- **Simulate end-of-month**: remaining WFH allowance, in-office requirement, deficit/surplus

Rules engine highlights:

- WFH allowance is **configurable** (`wfhHoursPerBusinessDay`, default **1.5** h per business day). Calendar-based: leave does not reduce the budget, matching Praise.
- WFH over quota does not count toward the balance.
- Paid leave reduces required hours; unpaid leave doesn't; half-day leave = half a day.
- Everything is recomputed locally and compared against Praise's server summary — any mismatch is flagged in the UI.

## Setup

```bash
make setup
uv run python -m praison config   # prompts for praise URL / email / password / rates
make run                          # http://localhost:8000
```

Config lives in `~/.config/praison/config.ini` (or env vars `PRAISE_URL`, `PRAISE_EMAIL`, `PRAISE_PASSWORD`, `PRAISE_HOURS_PER_DAY`, `PRAISON_WFH_HOURS_PER_BUSINESS_DAY`). Planned days are stored in `~/.config/praison/planning.db` (SQLite) — or in Postgres when `DB_HOST` is set (with `DB_NAME`, `DB_USER`, `DB_PASS`).

## Usage

- Stats panel: WFH used/quota, office actual/required, EoM deficit/surplus, suggested clock-out time for today.
- Tap any future working day to plan it (office hours, WFH hours, full/half-day leave, note).
- Praise data is cached server-side and refreshed every 10 minutes (or via ⟳).

## Docker

```bash
docker compose up --build
```

Mount a volume at `/data` for config + planning DB persistence (see `compose.yaml`).

## Development

```bash
make test
make lint
make lint/fix
```
