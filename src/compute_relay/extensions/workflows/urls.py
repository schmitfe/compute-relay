from django.urls import path
from . import views

urlpatterns = [
    path("api/v1/projects/<uuid:project_id>/workflow-revisions", views.revisions),
    path("api/v1/workflow-revisions/<uuid:revision_id>/runs", views.start),
    path("api/v1/workflow-runs/<uuid:run_id>", views.detail),
    path("api/v1/workflow-runs/<uuid:run_id>/advance", views.advance),
    path("api/v1/workflow-runs/<uuid:run_id>/cancel", views.cancel),
    path("projects/<uuid:project_id>/workflows/", views.portal),
    path("workflows/<uuid:run_id>/", views.review),
]
