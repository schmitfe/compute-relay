from django.conf import settings
from django.urls import include, path
from . import views
from . import management
from . import cluster_setup
from . import worker_management
from . import personal_connectors
from . import cluster_removal
from . import connector_setup
from . import cluster_checks
from . import live_updates
from . import object_lookup

urlpatterns = [
    path("healthz", views.health), path("health/live", views.health),
    path("", views.dashboard), path("login/", views.signin), path("logout/", views.signout),
    path("review/<uuid:job_id>/", views.review),
    path("ui/live/dashboard/", live_updates.snapshot, {"kind": "dashboard"}),
    path("ui/live/<str:kind>/<uuid:object_id>/", live_updates.snapshot),
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
    path("clusters/setup/<uuid:registration_id>/connect/", personal_connectors.download),
    path("clusters/<uuid:connection_id>/connect/", personal_connectors.download_connection),
    path("internal/v1/connectors/enroll", personal_connectors.enroll),
    path("internal/v1/connectors/tasks", connector_setup.tasks),
    path("internal/v1/connectors/tasks/<uuid:task_id>/report", connector_setup.report),
    path("clusters/<uuid:connection_id>/configuration/", connector_setup.configure),
    path("clusters/<uuid:connection_id>/directories/", connector_setup.directories),
    path("clusters/<uuid:connection_id>/test/", cluster_checks.check),
    path("clusters/setup/<uuid:registration_id>/remove/", cluster_removal.registration),
    path("clusters/<uuid:connection_id>/remove/", cluster_removal.connection),
    path("workers/new/", worker_management.worker_new),
    path("workers/<uuid:worker_id>/", worker_management.worker_detail),
    path("workers/<uuid:worker_id>/remove/", worker_management.worker_remove),
    path("clusters/<uuid:connection_id>/", management.cluster_detail),
    path("clusters/<uuid:connection_id>/discover/", management.cluster_discover),
    path("clusters/<uuid:connection_id>/toggle/", management.cluster_toggle),
    path("clusters/<uuid:connection_id>/environments/", management.environments),
    path("clusters/<uuid:connection_id>/environments/new/", management.environment_new),
    path("environments/", management.environments), path("environments/new/", management.environment_new),
    path("api/v1/connections", views.connections),
    path("api/v1/context", views.project_context),
    path("api/v1/capabilities", views.capabilities),
    path("api/v1/projects/<uuid:project_id>/allowance", views.allowance),
    path("api/v1/projects/<uuid:project_id>/objects", views.objects),
    path("api/v1/projects/<uuid:project_id>/objects/lookup", object_lookup.lookup),
    path("api/v1/jobs", views.jobs), path("api/v1/jobs/<uuid:job_id>", views.job_detail),
    path("api/v1/jobs/<uuid:job_id>/package", views.package), path("api/v1/jobs/<uuid:job_id>/prepare", views.prepare),
    path("api/v1/jobs/<uuid:job_id>/revisions", views.revisions),
    path("api/v1/jobs/<uuid:job_id>/execution-requests", views.execution_requests),
    path("api/v1/jobs/<uuid:job_id>/execution-requests/<uuid:execution_id>/cancel", views.cancel_execution_request),
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
