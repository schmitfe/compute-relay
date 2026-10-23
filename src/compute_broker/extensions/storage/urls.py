from django.urls import path

from . import views

urlpatterns = [
    path("storage/targets/", views.targets, name="storage_targets"),
    path("storage/targets/new/", views.target_new, name="storage_target_new"),
    path("storage/targets/directories/<uuid:connection_id>/", views.target_directories, name="storage_target_directories"),
    path("storage/targets/registrations/<uuid:registration_id>/", views.target_registration, name="storage_target_registration"),
    path("storage/pools/<uuid:pool_id>/probes/", views.pool_probes, name="storage_pool_probes"),
    path("projects/<uuid:project_id>/storage/", views.project_storage, name="project_storage"),
    path("projects/<uuid:project_id>/storage/targets/new/", views.target_new, name="project_storage_target_new"),
    path("projects/<uuid:project_id>/storage/targets/directories/<uuid:connection_id>/", views.target_directories, name="project_storage_directories"),
    path("projects/<uuid:project_id>/storage/targets/registrations/<uuid:registration_id>/", views.target_registration, name="project_storage_registration"),
    path("projects/<uuid:project_id>/storage/targets/<uuid:target_id>/adopt/", views.target_adopt, name="project_storage_adopt"),
    path("projects/<uuid:project_id>/storage/pools/<uuid:pool_id>/probes/", views.pool_probes, name="project_storage_pool_probes"),
    path("api/v1/projects/<uuid:project_id>/storage", views.storage_inventory, name="storage_inventory"),
    path("api/v1/projects/<uuid:project_id>/storage/plan", views.storage_plan, name="storage_plan"),
    path("internal/v1/extensions/storage/pools/<uuid:pool_id>/report", views.capacity_report, name="storage_capacity_report"),
]
