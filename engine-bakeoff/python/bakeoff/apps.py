from django.apps import AppConfig


class BakeoffConfig(AppConfig):
    """The engine bake-off's workload code (Foundation-written)."""

    name = "bakeoff"

    def ready(self):
        # Importing registers the bake-off's rules with the rule runner.
        from bakeoff import rules  # noqa: F401
