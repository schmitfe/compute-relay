from django.conf import settings
from django.urls import include, path
from . import views
from . import management
from . import cluster_setup

urlpatterns = [
    path("healthz", views.health), path("health/live", views.health),
    path("", views.dashboard), path("login/", views.signin), path("logout/", views.signout),
    path("review/<uuid:job_id>/", views.review),
    path("projects/new/", management.project_new), path("projects/<uuid:project_id>/", management.project_detail),
    path("projects/<uuid:project_id>/keys/new/", management.key_new),
    path("projects/<uuid:project_id>/keys/<uuid:key_id>/rotate/", management.key_rotate),
    path("projects/<uuid:project_id>/keys/<uuid:key_id>/revoke/", management.key_revoke),
    path("projects/<uuid:project_id>/keys/<uuid:key_id>/edit/", management.key_edit),
    path("clusters/", management.clusters), path("clusters/new/", management.cluster_new),
    path("clusters/setup/<uuid:registration_id>/", cluster_setup.detail),
    path("clusters/setup/<uuid:registration_id>/edit/", cluster_setup.edit),
    path("clusters/setup/<uuid:registration_id>/bind/", cluster_setup.bind),
    path("clusters/setup/<uuid:registration_id>/worker-config/", cluster_setup.worker_config),
    path("clusters/<uuid:connection_id>/", management.cluster_detail),
    path("clusters/<uuid:connection_id>/discover/", management.cluster_discover),
    path("clusters/<uuid:connection_id>/toggle/", management.cluster_toggle),
    path("clusters/<uuid:connection_id>/environments/", management.environments),
    path("clusters/<uuid:connection_id>/environments/new/", management.environment_new),
    path("environments/", management.environments), path("environments/new/", management.environment_new),
    path("api/v1/connections", views.connections),
    path("api/v1/capabilities", views.capabilities),
    path("api/v1/projects/<uuid:project_id>/allowance", views.allowance),
    path("api/v1/projects/<uuid:project_id>/objects", views.objects),
    path("api/v1/jobs", views.jobs), path("api/v1/jobs/<uuid:job_id>", views.job_detail),
    path("api/v1/jobs/<uuid:job_id>/package", views.package), path("api/v1/jobs/<uuid:job_id>/prepare", views.prepare),
    path("api/v1/jobs/<uuid:job_id>/revisions", views.revisions),
    path("api/v1/jobs/<uuid:job_id>/execution-requests", views.execution_requests),
    path("api/v1/jobs/<uuid:job_id>/decisions", views.decisions),
    path("api/v1/attempts/<uuid:attempt_id>/cancel", views.cancel), path("api/v1/attempts/<uuid:attempt_id>/logs", views.logs),
    path("api/v1/attempts/<uuid:attempt_id>/artifacts", views.artifacts), path("api/v1/artifacts/<uuid:artifact_id>/content", views.artifact_content),
    path("internal/v1/lease", views.lease), path("internal/v1/attempts/<uuid:attempt_id>/dispatch", views.dispatch),
    path("internal/v1/worker/profiles", views.worker_profiles),
    path("internal/v1/connections/<uuid:connection_id>/report", views.discovery_report),
    path("internal/v1/attempts/<uuid:attempt_id>/report", views.report),
]

from compute_broker.extensions import EXTENSIONS
urlpatterns += [path("", include(EXTENSIONS[name][0] + ".urls")) for name in settings.BROKER_EXTENSIONS]
