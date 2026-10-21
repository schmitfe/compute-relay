from django.apps import AppConfig


class WorkflowsConfig(AppConfig):
    name = "compute_broker.extensions.workflows"
    label = "broker_workflows"
    verbose_name = "Optional workflows"
