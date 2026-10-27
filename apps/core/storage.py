"""Manifest storage compatible with Jazzmin's theme-switcher directory URL."""
from django.contrib.staticfiles.storage import ManifestStaticFilesStorage


class AdminStaticFilesStorage(ManifestStaticFilesStorage):
    def stored_name(self, name):
        # Jazzmin 3.0.5 passes this directory to {% static %}; directories have
        # no manifest hash. Real asset files must still exist in the manifest.
        if name == "vendor/bootswatch":
            return name
        return super().stored_name(name)
