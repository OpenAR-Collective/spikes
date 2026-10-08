# Adapted from the HiveAR prototype by Andy Troiano
# (https://github.com/atroiano/platform-design, commit 361b6aa,
# prototype/hivear/apps.py), Apache License 2.0.
# Modified by The OpenAR Collective contributors.
# Changes: no longer imports hivear.smoke, the pre-cutover smoke probes of the
# blue-green release procedure, which stay in the prototype.
from django.apps import AppConfig


class HiveARConfig(AppConfig):
    name = "hivear"

    def ready(self):
        # Importing registers the module's command handlers and projections.
        from hivear import handlers, projections  # noqa: F401
        from wax import release

        release.register_component("hivear", release.RELEASE, release.MINIMUM_CODE_RELEASE, ["hivear"])
