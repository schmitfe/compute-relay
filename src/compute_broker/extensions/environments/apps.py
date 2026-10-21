from django.apps import AppConfig


class EnvironmentsConfig(AppConfig):
    name = "compute_broker.extensions.environments"
    label = "broker_environments"
    verbose_name = "Reusable environment builds"
    default_auto_field = "django.db.models.BigAutoField"
