from django.core.management.base import BaseCommand

from hivear.models import BalanceBucketDefinition

# Stands in for a debt-type module. HIVE-0005 has the framework ship no
# buckets of its own; these exist only so the draft can be exercised.
BUCKETS = [
    ("interest", "Interest", 10, True),
    ("fee", "Fees", 20, False),
    ("principal", "Principal", 30, False),
]


class Command(BaseCommand):
    help = "Load a sample lending bucket registry."

    def handle(self, *args, **options):
        for code, label, priority, bears_interest in BUCKETS:
            BalanceBucketDefinition.objects.update_or_create(
                code=code,
                defaults={
                    "label": label,
                    "waterfall_priority": priority,
                    "bears_interest": bears_interest,
                },
            )
        self.stdout.write(f"{len(BUCKETS)} bucket definitions loaded")
