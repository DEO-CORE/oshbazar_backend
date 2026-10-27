"""Manifest storage compatible with Jazzmin's theme-switcher directory URL."""
from django.contrib.staticfiles.storage import ManifestStaticFilesStorage


class AdminStaticFilesStorage(ManifestStaticFilesStorage):
    def stored_name(self, name):
        # Jazzmin 3.0.5 passes this directory to {% static %}; directories have
        # no manifest hash. Real asset files must still exist in the manifest.
        if name == "vendor/bootswatch":
            return name
        return super().stored_name(name)


from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible


@deconstructible
class VercelBlobStorage(Storage):
    """Private persistent meter photos, delivered through the authenticated API."""

    def _save(self, name, content):
        import mimetypes
        from vercel.blob.sync import put_blob

        result = put_blob(
            name, content.read(), access="private", add_random_suffix=True,
            content_type=getattr(content, "content_type", None) or mimetypes.guess_type(name)[0],
        )
        return result.pathname

    def _open(self, name, mode="rb"):
        from vercel.blob import BlobNotFoundError
        from vercel.blob.sync import get_blob

        if mode not in {"r", "rb"}:
            raise ValueError("Blob files are read-only; use save() to upload a new file")
        try:
            result = get_blob(name, access="private")
        except BlobNotFoundError as exc:
            raise FileNotFoundError(name) from exc
        return ContentFile(result.body, name=name)

    def exists(self, name):
        from vercel.blob import BlobNotFoundError
        from vercel.blob.sync import head_blob

        try:
            head_blob(name)
        except BlobNotFoundError:
            return False
        return True

    def size(self, name):
        from vercel.blob.sync import head_blob
        return head_blob(name).size

    def url(self, name):
        from vercel.blob.sync import head_blob
        return head_blob(name).url

    def delete(self, name):
        from vercel.blob.sync import delete_blob
        delete_blob(name)

    def get_available_name(self, name, max_length=None):
        # Blob adds a random suffix atomically, without a separate HEAD request.
        return name
