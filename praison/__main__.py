"""Entry point.

``python -m praison`` runs the open-access multi-tenant web app.
``python -m praison config`` writes a legacy ``config.ini`` whose credentials are
seeded as the first user on startup (used to migrate the previous single-tenant
deployment; new users just log in through the web form).
"""

import getpass
import logging
import sys

import uvicorn

from praison.config import Config
from praison.database import Store, create_database
from praison.praise.session import normalize_url

logger = logging.getLogger(__name__)


def configure() -> Config:
    """Interactively prompt for configuration and save it."""
    url = ""
    while not url:
        sys.stdout.write("Praise URL: ")
        sys.stdout.flush()
        url = input().strip()
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


def _seed_legacy_user(db: Store) -> None:
    """Migrate a previous single-tenant deployment: if legacy ``PRAISE_*`` creds
    are configured and not yet a user, register them and claim orphaned plans."""
    config = Config.from_env() or Config.load()
    if config is None:
        return
    url = normalize_url(config.praise_url)
    if db.get_user_by_identity(url, config.praise_email):
        return
    # No password is stored: the seed only establishes the ownership row and
    # claims orphaned plans. The user supplies their password at login, where it
    # is kept in the session cookie only.
    user = db.create_user(
        url,
        config.praise_email,
        config.hours_per_day,
        config.wfh_hours_per_business_day,
    )
    db.claim_legacy_plans(user.id)
    logger.info("seeded legacy user %s and claimed existing plans", config.praise_email)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "config":
        configure()
        return

    from praison.app import create_app

    db = create_database()
    _seed_legacy_user(db)
    uvicorn.run(create_app(db=db), host="0.0.0.0", port=8000)  # noqa: S104


if __name__ == "__main__":
    main()
