from django.urls import path

from . import views

urlpatterns = [
    path("storage/targets/", views.targets, name="storage_targets"),
    path("storage/targets/new/", views.target_new, name="storage_target_new"),
    path("storage/pools/<uuid:pool_id>/probes/", views.pool_probes, name="storage_pool_probes"),
    path("projects/<uuid:project_id>/storage/", views.project_storage, name="project_storage"),
    path("api/v1/projects/<uuid:project_id>/storage", views.storage_inventory, name="storage_inventory"),
    path("api/v1/projects/<uuid:project_id>/storage/plan", views.storage_plan, name="storage_plan"),
    path("internal/v1/extensions/storage/pools/<uuid:pool_id>/report", views.capacity_report, name="storage_capacity_report"),
]
