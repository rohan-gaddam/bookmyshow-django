from django.urls import path
from . import dashboard_views

urlpatterns = [
    path("", dashboard_views.admin_dashboard, name="admin_dashboard"),
    path("export.csv", dashboard_views.admin_dashboard_csv, name="admin_dashboard_csv"),
]
