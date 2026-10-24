from django.apps import AppConfig


class WorkflowsConfig(AppConfig):
    name = "compute_relay.extensions.workflows"
    label = "broker_workflows"
    verbose_name = "Optional workflows"
