import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
os.chdir(BASE_DIR)

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "bookmyseat.settings")

import django
django.setup()

from django.db import connections
connections.close_all()
connections.databases["default"] = {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": str(BASE_DIR / "db.sqlite3"),
}

from movies.models import Movie

for movie in Movie.objects.all():
    image = movie.image
    print(f"Movie: {movie.name}")
    print(f"Image path: {image.name if image else 'NONE'}")
    print(f"Image URL: {image.url if image else 'NONE'}")
    if image:
        print(f"Local file exists: {(BASE_DIR / 'media' / image.name).is_file()}")
    print("-" * 45)
