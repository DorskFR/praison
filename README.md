# praison

Self-hosted web app for planning your hours on a Praise time-tracking instance: plan future office / WFH / leave days and see a live monthly projection against your workplace rules.

| Dark | Light |
| --- | --- |
| ![Planner — dark theme](docs/screenshot-dark.png) | ![Planner — light theme](docs/screenshot-light.png) |

## Run with Docker (recommended)

```bash
docker compose up
```

That's the whole setup — one command, no files to mount, no keys to generate. Open <http://localhost:24601>, enter your **Praise URL**, then **authorize praison in Praise**: praison shows a short code and a link, you approve it in your own Praise session (where 2FA and the captcha are handled), and praison is signed in. No Praise password is ever entered into or stored by praison — only the resulting access token, encrypted, per user. The encryption key and Postgres data are created once and persist in named Docker volumes.

Or run the published image directly (no Postgres — falls back to SQLite stored in the volume):

```bash
docker run -d -p 24601:24601 -v praison-data:/data ghcr.io/dorskfr/praison:latest
```

## Run locally

```bash
make setup
make run          # http://localhost:24601
```

Local mode stores everything (SQLite database, encryption key) under `~/.config/praison/`.

## Settings

Per-user options live behind the ⚙ button:

- **Hours per day** — your daily working-hour target.
- **WFH hours per business day** — your remote-work allowance per business day.

## Development

```bash
make test
make lint
make lint/fix
```

## License

[WTFPL](LICENSE) — do what the fuck you want to.
