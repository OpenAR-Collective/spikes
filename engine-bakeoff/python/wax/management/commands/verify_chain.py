from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from wax import store


class Command(BaseCommand):
    help = "Recompute every per-aggregate hash chain in core.event (WAX-0013)."

    def handle(self, *args, **options):
        with connection.cursor() as cursor:
            cursor.execute("SELECT DISTINCT aggregate_pk_ref FROM core.event")
            aggregate_pks = [row[0] for row in cursor.fetchall()]
        broken = 0
        for aggregate_pk in aggregate_pks:
            for problem in store.verify_stream(aggregate_pk):
                broken += 1
                self.stderr.write(f"{aggregate_pk}: {problem}")
        if broken:
            raise CommandError(f"{broken} problems across {len(aggregate_pks)} aggregates")
        self.stdout.write(f"{len(aggregate_pks)} aggregate chains verified")
