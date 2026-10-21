from django.apps import AppConfig


class BrokerConfig(AppConfig):
    name = "compute_broker.control"
    label = "broker"
    default_auto_field = "django.db.models.BigAutoField"
