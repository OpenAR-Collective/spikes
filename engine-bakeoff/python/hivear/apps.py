from django.apps import AppConfig


class HiveARConfig(AppConfig):
    name = "hivear"

    def ready(self):
        # Importing registers the module's command handlers and projections.
        from hivear import handlers, projections, smoke  # noqa: F401
        from wax import release

        release.register_component("hivear", release.RELEASE, release.MINIMUM_CODE_RELEASE, ["hivear"])
