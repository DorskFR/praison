"""Configuration management."""

import configparser
import os
from dataclasses import dataclass
from pathlib import Path

_XDG_CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME")
_CONFIG_HOME = Path(_XDG_CONFIG_HOME) if _XDG_CONFIG_HOME else Path.home() / ".config"
DEFAULT_CONFIG_DIR = _CONFIG_HOME / "praison"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.ini"
DEFAULT_DB_PATH = DEFAULT_CONFIG_DIR / "planning.db"
DEFAULT_SESSION_PATH = DEFAULT_CONFIG_DIR / "session"


@dataclass
class Config:
    """Praise authentication and rules configuration."""

    praise_url: str
    praise_email: str
    praise_password: str
    hours_per_day: int = 8
    wfh_hours_per_business_day: float = 1.5

    @classmethod
    def from_env(cls) -> "Config | None":
        """Load configuration from environment variables."""
        try:
            return cls(
                praise_url=os.environ["PRAISE_URL"],
                praise_email=os.environ["PRAISE_EMAIL"],
                praise_password=os.environ["PRAISE_PASSWORD"],
                hours_per_day=int(os.environ.get("PRAISE_HOURS_PER_DAY", "8")),
                wfh_hours_per_business_day=float(
                    os.environ.get("PRAISON_WFH_HOURS_PER_BUSINESS_DAY", "1.5")
                ),
            )
        except KeyError:
            return None

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG_PATH) -> "Config | None":
        """Load configuration from file."""
        if not path.is_file():
            return None

        config = configparser.ConfigParser(interpolation=None)
        config.read(path)
        section = config["praise"]
        return cls(
            praise_url=section["url"],
            praise_email=section["email"],
            praise_password=section["password"],
            hours_per_day=section.getint("hoursPerDay", fallback=8),
            wfh_hours_per_business_day=section.getfloat("wfhHoursPerBusinessDay", fallback=1.5),
        )

    def save(self, path: Path = DEFAULT_CONFIG_PATH) -> None:
        """Save configuration to file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        config = configparser.ConfigParser(interpolation=None)
        config["praise"] = {
            "url": self.praise_url,
            "email": self.praise_email,
            "password": self.praise_password,
            "hoursPerDay": str(self.hours_per_day),
            "wfhHoursPerBusinessDay": str(self.wfh_hours_per_business_day),
        }
        with path.open("w") as config_file:
            config.write(config_file)
        path.chmod(0o600)
