import os
import sys
import threading
import time
import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class MoviesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'movies'

    def ready(self):
        # Run only with Django's development server, never other commands.
        if len(sys.argv) < 2 or sys.argv[1] != 'runserver':
            return

        # With Django's auto-reloader, start only in the serving child process.
        if os.environ.get('RUN_MAIN') != 'true' and '--noreload' not in sys.argv:
            return

        # Prevent duplicate workers in this process.
        if getattr(self, '_cleanup_worker_started', False):
            return

        self._cleanup_worker_started = True

        def cleanup_loop():
            from django.core.management import call_command

            while True:
                try:
                    call_command('cleanup_expired_reservations', verbosity=0)
                except Exception:
                    logger.exception('Expired reservation cleanup failed.')

                time.sleep(60)

        threading.Thread(
            target=cleanup_loop,
            name='bookmyshow-expired-reservation-cleanup',
            daemon=True,
        ).start()
