import logging
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from wax import projections, release

logger = logging.getLogger("wax.projections")


class Command(BaseCommand):
    help = "Keep read models current from core.event (WAX-0001)."

    def add_arguments(self, parser):
        parser.add_argument("--poll-seconds", type=float, default=0.25)
        parser.add_argument("--heartbeat-seconds", type=float, default=5)

    def handle(self, *args, poll_seconds, heartbeat_seconds, **options):
        logger.info("Projection worker started: release %s, color %s", release.RELEASE, release.COLOR)
        registered, last_beat = False, 0.0
        while True:
            close_old_connections()
            try:
                # Rechecked continuously: the schema can move past this
                # release while the worker is running.
                release.assert_schema_supports_this_code(max_age_seconds=5)
                if not registered:
                    projections.register_checkpoints()
                    registered = True
                if time.monotonic() - last_beat >= heartbeat_seconds:
                    projections.beat()
                    last_beat = time.monotonic()
                handled = projections.run_once()
            except release.IncompatibleSchema as exc:
                logger.error("Not processing: %s", exc)
                handled = 0
                time.sleep(5)
            except Exception:
                # A database restart or a cutover must not end the worker, and
                # whatever the reader knew about the old connection is void.
                logger.exception("Projection pass failed; retrying")
                projections.reset()
                handled = 0
                time.sleep(2)
            if handled == 0:
                time.sleep(poll_seconds)
