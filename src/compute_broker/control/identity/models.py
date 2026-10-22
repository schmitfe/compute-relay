"""Stable external subjects never attach accounts by username or email."""

from django.db import models
from django.utils import timezone


class AuthenticationIdentity(models.Model):
    user = models.OneToOneField("broker.User", on_delete=models.PROTECT, related_name="authentication_identity")
    provider_id = models.CharField(max_length=40)
    subject = models.CharField(max_length=36)
    login_name = models.CharField(max_length=150)
    display_name = models.CharField(max_length=255, blank=True)
    enabled = models.BooleanField(default=True)
    directory_active = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    provider_config_sha256 = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = "broker"
        constraints = [models.UniqueConstraint(fields=["provider_id", "subject"], name="identity_provider_subject")]
