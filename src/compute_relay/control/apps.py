from django.apps import AppConfig


class BrokerConfig(AppConfig):
    name = "compute_relay.control"
    label = "broker"
    default_auto_field = "django.db.models.BigAutoField"
