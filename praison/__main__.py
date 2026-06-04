"""Entry point: `python -m praison` runs the web app, `python -m praison config` reconfigures."""

import getpass
import sys

import uvicorn

from praison.config import Config


def configure() -> Config:
    """Interactively prompt for configuration and save it."""
    sys.stdout.write("Praise URL [praise.pafin.com]: ")
    sys.stdout.flush()
    url = input().strip() or "praise.pafin.com"
    sys.stdout.write("Praise email: ")
    sys.stdout.flush()
    email = input().strip()
    password = getpass.getpass("Praise password: ")
    sys.stdout.write("Hours per day [8]: ")
    sys.stdout.flush()
    hours = int(input().strip() or "8")
    sys.stdout.write("WFH hours per business day [1.5]: ")
    sys.stdout.flush()
    wfh = float(input().strip() or "1.5")
    config = Config(
        praise_url=url,
        praise_email=email,
        praise_password=password,
        hours_per_day=hours,
        wfh_hours_per_business_day=wfh,
    )
    config.save()
    return config


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "config":
        configure()
        return

    config = Config.from_env() or Config.load()
    if config is None:
        config = configure()

    from praison.app import create_app

    uvicorn.run(create_app(config), host="0.0.0.0", port=8000)  # noqa: S104


if __name__ == "__main__":
    main()
