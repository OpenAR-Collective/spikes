import json

from django.core.management.base import BaseCommand, CommandError

from wax import projections


class Command(BaseCommand):
    help = "Show projection positions, clear a recorded failure, or adopt an unshipped definition."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["status", "retry", "accept-definition"])
        parser.add_argument("name", nargs="?")

    def handle(self, *args, action, name, **options):
        projections.register_checkpoints()
        if action == "retry":
            if not name:
                raise CommandError("retry needs a projection name")
            projections.retry(name)
        if action == "accept-definition":
            if not name:
                raise CommandError("accept-definition needs a projection name")
            projections.accept_definition(name)
        self.stdout.write(json.dumps(projections.status(), indent=2, default=str))
