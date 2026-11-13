import argparse
import logging
from pathlib import Path
from sys import exit
from signal import signal, SIGINT, SIGTERM
from time import sleep

from src.config import Settings
from src.loader import SpecErrors, load_registry
from src.processor import Processor


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Analytics processor: enrich events, raise alerts, serve a dashboard API.")
    ap.add_argument("--ingest-db", type=Path, help="ingestion sqlite (default ../data/db.sqlite)")
    ap.add_argument("--db", type=Path, dest="processor_db", help="processor sqlite (default ../data/processor.sqlite)")
    ap.add_argument("--applications", type=Path, dest="applications_dir", help="folder of application XML files")
    ap.add_argument("--host", dest="api_host", help="API bind address (default 0.0.0.0)")
    ap.add_argument("--port", dest="api_port", type=int, help="API port (default 8000)")
    ap.add_argument("--no-api", action="store_true", help="only process, do not serve the HTTP API")
    ap.add_argument("--check", action="store_true", help="validate applications/*.xml and exit")
    ap.add_argument("--once", action="store_true", help="process the current backlog, then exit (no API)")
    ap.add_argument("--log-level", dest="log_level", help="DEBUG, INFO, WARNING ...")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    overrides = {k: v for k, v in vars(args).items() if k not in ("no_api", "check", "once")}
    if overrides.get("log_level"):
        overrides["log_level"] = overrides["log_level"].upper()
    settings: Settings = Settings.from_env().with_overrides(**overrides)

    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    if args.check:
        try:
            registry = load_registry(settings.applications_dir)
        except SpecErrors as exc:
            print(exc)
            return 2
        for event in registry.events():
            print(f"ok  {event.key}: {len(event.dimensions)} dimensions, "
                  f"{len(event.enrichment)} enrichments, {len(event.alerts)} alert rules")
        return 0

    proc: Processor = Processor(settings)

    if args.once:
        try:
            count = proc.drain()
        except SpecErrors as exc:
            logging.getLogger("processor").error("%s", exc)
            return 2
        status = proc.status()
        print(f"processed {count} rows: {status['processed']} handled, {status['ignored']} ignored, "
              f"{status['alerts_created']} alerts created, {status['errors']} errors")
        proc.stop()
        return 0

    signal(SIGINT, lambda *args, **kwargs: proc.stop())
    signal(SIGTERM, lambda *args, **kwargs: proc.stop())

    try:
        proc.start()
    except SpecErrors as exc:
        logging.getLogger("processor").error("%s", exc)
        return 2

    api = None
    if not args.no_api:
        from src.api.server import ApiServer
        api = ApiServer(proc, settings)
        try:
            api.start()
        except RuntimeError as exc:
            logging.getLogger("processor").error("%s", exc)
            proc.stop()
            return 1
        logging.getLogger("processor").info("API on http://%s:%d  (docs: /docs)", settings.api_host, settings.api_port)

    while proc.running:
        sleep(0.1)

    if api is not None:
        api.stop()
    return (0)


if (__name__ == "__main__"):
    exit(main())
