from django.urls import path
from . import views

urlpatterns = [
    path("api/v1/projects/<uuid:project_id>/investigations", views.investigations),
    path("api/v1/investigations/<uuid:investigation_id>", views.detail),
    path("api/v1/investigations/<uuid:investigation_id>/jobs", views.add_jobs),
    path("api/v1/investigations/<uuid:investigation_id>/finish", views.finish),
    path("projects/<uuid:project_id>/investigations/", views.portal),
    path("investigations/<uuid:investigation_id>/", views.review),
]
