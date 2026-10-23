"""Explain independent review gates without granting execution authority."""


def approval_requirements(policy, key_execution_mode, cluster_configuration, *, within_envelope=None):
    """Return current reasons plus mixed-policy planning conditions.

    ``None`` means no concrete allocation has been compared with the project
    envelope yet. Missing legacy cluster flags retain the existing admission
    meaning: an explicit False requires review. Authorization, current identity,
    available funds and scheduler checks remain separate.
    """
    if not isinstance(policy, dict) or not isinstance(cluster_configuration, dict):
        raise ValueError("Expected project policy and cluster configuration objects.")
    if within_envelope is not None and type(within_envelope) is not bool:
        raise ValueError("Expected a boolean envelope comparison or None.")
    profile = cluster_configuration.get("site_profile", {})
    if not isinstance(profile, dict):
        raise ValueError("Expected a cluster site profile object.")
    reasons, conditional = [], []

    def reason(code, source, message):
        return {"code": code, "source": source, "message": message}

    if policy.get("mode") == "approval_required":
        reasons.append(reason("PROJECT_REVIEW_REQUIRED", "project", "The project is set to review every execution."))
    if key_execution_mode == "approval_required":
        reasons.append(reason("KEY_REVIEW_REQUIRED", "key", "This API key requires confirmation for every execution."))
    if profile.get("automatic_execution_allowed") is False:
        reasons.append(reason("CLUSTER_REVIEW_REQUIRED", "cluster",
            "This cluster configuration requires confirmation for every execution. Its owner can allow automatic execution in Cluster configuration."))
    if policy.get("mode") == "mixed":
        if within_envelope is False:
            reasons.append(reason("PROJECT_ENVELOPE_REVIEW_REQUIRED", "project",
                "This mixed-policy request exceeds the project's per-job limits and needs human review."))
        elif within_envelope is None:
            conditional.append(reason("PROJECT_ENVELOPE_REVIEW_REQUIRED", "project",
                "In mixed mode, requests outside the project's per-job limits require human review."))
    return {"requires_human_approval": bool(reasons), "approval_reasons": reasons,
        "conditional_approval_reasons": conditional,
        "conditional_approval_notice": conditional[0]["message"] if conditional else None}
