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
export PRAISON_SECRET_KEY=$(uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
make run                          # http://localhost:8000
```

praison is **open / multi-tenant**: anyone reaches a login screen and signs in with their **Praise URL + email + password**. On the first successful login (validated against that Praise server) a local account is created — keyed on `(praise_url, email)`, no separate praison password. Multiple users and multiple Praise servers are supported side by side; all storage is scoped per user.

The Praise password is **never stored server-side**: it rides in the signed session cookie (Fernet-encrypted) and is decrypted in memory only to replay to the Praise server on each fetch. The database holds no credential material — only ownership (`(praise_url, email)` → account) and per-user settings. Logging out or letting the cookie expire drops the password; the user simply logs in again. Set `PRAISON_SECRET_KEY` to a urlsafe-base64 32-byte Fernet key (used both to sign the session cookie and to encrypt the password inside it); locally, one is generated and persisted at `~/.config/praison/secret.key` if unset. **Rotating or losing this key invalidates all active sessions** (users log in again).

Planned days and accounts are stored in `~/.config/praison/planning.db` (SQLite) — or in Postgres when `DB_HOST` is set (with `DB_NAME`, `DB_USER`, `DB_PASS`).

### Migrating a legacy single-tenant deployment

If `PRAISE_URL` / `PRAISE_EMAIL` / `PRAISE_PASSWORD` (or a `~/.config/praison/config.ini` written by `python -m praison config`) are present, that identity is seeded as the first user on startup and any pre-existing (un-scoped) planned days are claimed by it.

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
