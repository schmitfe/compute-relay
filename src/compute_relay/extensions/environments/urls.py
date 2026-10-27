from django.urls import path

from . import views

urlpatterns = [
    path("api/v1/environments/<uuid:environment_id>/inventory", views.inventory),
    path("api/v1/environments/<uuid:environment_id>/inspect", views.inspect),
    path("api/v1/environments/<uuid:environment_id>/packages", views.packages),
    path("api/v1/projects/<uuid:project_id>/environment-builds", views.builds),
    path("api/v1/projects/<uuid:project_id>/environment-build-policy", views.policy_api),
    path("api/v1/environment-builds/<uuid:build_id>", views.detail),
    path("api/v1/environment-builds/<uuid:build_id>/prepare", views.prepare),
    path("api/v1/environment-builds/<uuid:build_id>/execution-requests", views.execute),
    path("api/v1/environment-builds/<uuid:build_id>/promote", views.promote),
    path("projects/<uuid:project_id>/environment-builds/", views.portal),
    path("projects/<uuid:project_id>/environment-build-policy/", views.policy_portal),
    path("environment-builds/<uuid:build_id>/promote/", views.promote),
]
