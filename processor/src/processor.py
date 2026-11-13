"""The processor: a worker thread that tails the ingestion table and runs the engine on it."""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from .config import Settings
from .engine import Engine
from .loader import SpecErrors, fingerprint, load_registry
from .notifier import Notifier
from .reader import IngestReader, SourceUnavailable
from .store import Store, now_ms

log = logging.getLogger(__name__)


class Processor(object):
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or Settings.from_env()
        self.running = False
        self.store: Optional[Store] = None
        self.engine: Optional[Engine] = None
        self.registry_version = ""
        self.metrics: dict = {}

        self._reader: Optional[IngestReader] = None
        self._notifier: Optional[Notifier] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._xml_fingerprint: tuple = ()

    def open(self) -> None:
        """Open the stores and load the application definitions (raises :class:`SpecErrors` when
        they are invalid) without starting the worker. :meth:`start` calls it for you."""
        if self.store is not None:
            return
        s = self.settings
        self.store = Store(s.processor_db)
        self._reader = IngestReader(s.ingest_db)
        self._notifier = Notifier(s)
        self.engine = Engine(self.store, s)
        self.metrics = {"started_at_ms": now_ms(), "processed": 0, "ignored": 0, "errors": 0,
                        "alerts_created": 0, "batches": 0, "last_event_ts_ms": None,
                        "last_error": None, "source_available": False, "lag": None}

        registry = load_registry(s.applications_dir)
        self._xml_fingerprint = fingerprint(s.applications_dir)
        self.registry_version = registry.version
        info = self.engine.apply(registry)
        log.info("loaded %d event definitions (version %s), %d alert rules",
                 len(registry.events()), registry.version, info["rules"])

    def start(self) -> None:
        """Open (if needed) and start the worker thread."""
        if self.running:
            return
        self.open()
        self._stop.clear()
        self.running = True
        self._thread = threading.Thread(target=self._run, name="processor", daemon=True)
        self._thread.start()

    def drain(self) -> int:
        """Synchronously process everything currently in the ingestion table. Returns the row count.
        Used by ``--once`` and by tests; do not call while the worker thread is running."""
        self.open()
        total = 0
        while True:
            handled = self._tick()
            if handled == 0:
                return total
            total += handled

    def stop(self) -> None:
        self.running = False
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=10)
        self._thread = None
        if self._notifier is not None:
            self._notifier.close()
        if self._reader is not None:
            self._reader.close()

    def __del__(self):
        try:
            self.stop()
        except Exception:
            pass

    def _run(self) -> None:
        s = self.settings
        last_reload = last_sweep = last_prune = time.monotonic()
        log.info("processor started: %s -> %s", s.ingest_db, s.processor_db)
        try:
            while not self._stop.is_set():
                try:
                    handled = self._tick()
                    self.metrics["source_available"] = True
                except SourceUnavailable as exc:
                    if self.metrics.get("source_available", True):
                        log.warning("ingestion database unavailable: %s", exc)
                    self.metrics["source_available"] = False
                    handled = 0
                    self._stop.wait(2.0)
                except Exception as exc:
                    log.exception("processing batch failed")
                    self.metrics["errors"] += 1
                    self.metrics["last_error"] = f"{type(exc).__name__}: {exc}"
                    handled = 0
                    self._stop.wait(2.0)

                now = time.monotonic()
                if s.reload_check_interval and now - last_reload >= s.reload_check_interval:
                    last_reload = now
                    if fingerprint(s.applications_dir) != self._xml_fingerprint:
                        self.reload()
                if now - last_sweep >= s.sweep_interval:
                    last_sweep = now
                    with self._lock:
                        self.engine.sweep()
                if s.retention_days and now - last_prune >= s.prune_interval:
                    last_prune = now
                    self._prune()
                if handled == 0:
                    self._stop.wait(s.poll_interval)
        finally:
            self.running = False
            log.info("processor stopped")

    def _tick(self) -> int:
        """Process one batch. Returns the number of rows handled (0 = caught up)."""
        with self._lock:
            cursor = self.store.get_cursor()
            rows = self._reader.fetch(cursor, self.settings.batch_size)
            if self._reader.replaced or (not rows and self._reader.max_id() < cursor):
                self._reader.replaced = False
                if cursor:
                    log.warning("ingestion database was replaced or reset: restarting from id 0")
                    self.store.reset_cursor(0)
                    cursor = 0
                    rows = self._reader.fetch(0, self.settings.batch_size)
            if not rows:
                self.metrics["lag"] = 0
                return 0

            items, ignored = [], 0
            for raw in rows:
                try:
                    event, candidates = self.engine.process(raw)
                except Exception as exc:
                    log.exception("event %s could not be processed", raw.event_id)
                    self.metrics["errors"] += 1
                    self.metrics["last_error"] = f"{raw.event_id}: {type(exc).__name__}: {exc}"
                    event, candidates = None, []
                if event is None:
                    ignored += 1
                items.append((event, candidates))

            created = self.store.write_batch(items, rows[-1].id)
            m = self.metrics
            m["batches"] += 1
            m["processed"] += len(rows) - ignored
            m["ignored"] += ignored
            m["alerts_created"] += len(created)
            last = max(rows, key=lambda r: r.timestamp_ms)
            m["last_event_ts_ms"] = last.timestamp_ms
            m["lag"] = max(self._reader.max_id() - rows[-1].id, 0)
        self._notifier.notify(created)
        return len(rows)

    def _prune(self) -> None:
        before = now_ms() - self.settings.retention_days * 86_400_000
        try:
            events, alerts = self.store.prune(before)
            if events or alerts:
                log.info("retention: removed %d events and %d resolved alerts", events, alerts)
        except Exception:
            log.exception("retention clean-up failed")

    def reload(self) -> dict:
        """Re-read ``applications/*.xml``. On any error the running definitions stay in place."""
        with self._lock:
            self._xml_fingerprint = fingerprint(self.settings.applications_dir)
            try:
                registry = load_registry(self.settings.applications_dir)
            except SpecErrors as exc:
                log.error("reload rejected, keeping version %s:\n%s", self.registry_version, exc)
                return {"ok": False, "version": self.registry_version, "errors": exc.errors}
            if registry.version == self.registry_version:
                return {"ok": True, "version": registry.version, "changed": False}
            info = self.engine.apply(registry)
            self.registry_version = registry.version
            log.info("reloaded application definitions: version %s", registry.version)
            return {"ok": True, "version": registry.version, "changed": True, **info}

    def status(self) -> dict:
        thread_alive = bool(self._thread and self._thread.is_alive())
        return {
            "running": self.running and thread_alive,
            "registry_version": self.registry_version,
            "cursor": self.store.get_cursor() if self.store else None,
            **self.metrics,
        }
