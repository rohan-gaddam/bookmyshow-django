import os
import tempfile
from django.core.files.storage import FileSystemStorage


class ServerlessFileSystemStorage(FileSystemStorage):
    """
    FileSystemStorage with resilient fallback for serverless runtimes (like Vercel).
    If MEDIA_ROOT is on a read-only filesystem, file writes transparently fallback
    to the writable temporary directory (/tmp) without raising OSError: [Errno 30].
    """

    def _get_tmp_storage(self):
        tmp_dir = os.path.join(tempfile.gettempdir(), 'media')
        return FileSystemStorage(location=tmp_dir)

    def _save(self, name, content):
        try:
            return super()._save(name, content)
        except OSError as exc:
            # Handle Read-only file system (Errno 30) or Permission denied (Errno 13)
            if getattr(exc, 'errno', None) in (30, 13) or 'read-only' in str(exc).lower():
                return self._get_tmp_storage()._save(name, content)
            raise

    def exists(self, name):
        if super().exists(name):
            return True
        tmp_dir = os.path.join(tempfile.gettempdir(), 'media')
        return os.path.exists(os.path.join(tmp_dir, name))

    def _open(self, name, mode='rb'):
        try:
            return super()._open(name, mode)
        except (FileNotFoundError, OSError):
            tmp_dir = os.path.join(tempfile.gettempdir(), 'media')
            if os.path.exists(os.path.join(tmp_dir, name)):
                return self._get_tmp_storage()._open(name, mode)
            raise
