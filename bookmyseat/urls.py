from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from movies.views import serve_media

urlpatterns = [
    path('admin/', admin.site.urls),
    path('users/', include('users.urls')),
    path('', include('users.urls')),
    path('admin-dashboard/', include('movies.dashboard_urls')),
    path('movies/', include('movies.urls')),
    path('media-file/<path:file_path>/', serve_media, name='serve_media'),
    path('media-file/<path:file_path>', serve_media),
]

if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
# Always serve media files so /media/ works on Vercel (DEBUG=False in production)
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
