from django.apps import AppConfig


class WaxConfig(AppConfig):
    name = "wax"

    def ready(self):
        from wax import release

        release.register_component("wax", release.RELEASE, release.MINIMUM_CODE_RELEASE, ["wax"])
