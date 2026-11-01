from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
urlpatterns = [
    path('admin/', admin.site.urls),
    path('users/', include('users.urls')),
    path('',include('users.urls')),
    path('admin-dashboard/', include('movies.dashboard_urls')),
    path('movies/', include('movies.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
# Always serve media files so /media/ works on Vercel (DEBUG=False in production)
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

