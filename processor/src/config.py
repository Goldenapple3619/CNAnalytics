"""Runtime settings. Everything can be overridden with ``PROCESSOR_*`` environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    return os.environ.get(f"PROCESSOR_{name}", default)


@dataclass(frozen=True)
class Settings:
    ingest_db: Path = ROOT.parent / "data" / "db.sqlite"
    processor_db: Path = ROOT.parent / "data" / "processor.sqlite"
    applications_dir: Path = ROOT / "applications"

    poll_interval: float = 0.5
    batch_size: int = 500
    reload_check_interval: float = 5
    sweep_interval: float = 30
    time_field: str = "timestamp_ms"
    retention_days: int = 30
    prune_interval: float = 3600

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_key: str = ""                 # when set, every /api/* call needs the X-API-Key header
    cors_origins: tuple[str, ...] = ("*",)

    webhook_url: str = ""             # POSTed (JSON) for every newly created alert
    webhook_timeout: float = 5.0

    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        d = cls()
        time_field = _env("TIME_FIELD", d.time_field)
        if time_field not in ("timestamp_ms", "received_at_ms"):
            raise ValueError("PROCESSOR_TIME_FIELD must be timestamp_ms or received_at_ms")
        return cls(
            ingest_db=Path(_env("INGEST_DB", str(d.ingest_db))),
            processor_db=Path(_env("DB", str(d.processor_db))),
            applications_dir=Path(_env("APPLICATIONS_DIR", str(d.applications_dir))),
            poll_interval=float(_env("POLL_INTERVAL", str(d.poll_interval))),
            batch_size=int(_env("BATCH_SIZE", str(d.batch_size))),
            reload_check_interval=float(_env("RELOAD_CHECK_INTERVAL", str(d.reload_check_interval))),
            sweep_interval=float(_env("SWEEP_INTERVAL", str(d.sweep_interval))),
            time_field=time_field,
            retention_days=int(_env("RETENTION_DAYS", str(d.retention_days))),
            prune_interval=float(_env("PRUNE_INTERVAL", str(d.prune_interval))),
            api_host=_env("API_HOST", d.api_host),
            api_port=int(_env("API_PORT", str(d.api_port))),
            api_key=_env("API_KEY", d.api_key),
            cors_origins=tuple(o.strip() for o in _env("CORS_ORIGINS", "*").split(",") if o.strip()),
            webhook_url=_env("WEBHOOK_URL", d.webhook_url),
            webhook_timeout=float(_env("WEBHOOK_TIMEOUT", str(d.webhook_timeout))),
            log_level=_env("LOG_LEVEL", d.log_level).upper(),
        )

    def with_overrides(self, **overrides) -> "Settings":
        """Return a copy; ``None`` values are ignored so CLI flags can be passed straight in."""
        return replace(self, **{k: v for k, v in overrides.items() if v is not None})
