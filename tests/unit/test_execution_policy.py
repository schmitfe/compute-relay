"""Independent review preferences are explained without widening authority."""

import pytest

from compute_relay.control.execution_policy import approval_requirements


@pytest.mark.parametrize("project_mode", ["approval_required", "budgeted", "mixed"])
@pytest.mark.parametrize("key_mode", ["inherit", "approval_required"])
@pytest.mark.parametrize("cluster_setting", [False, True, None])
@pytest.mark.parametrize("within", [False, True, None])
def test_every_independent_gate_and_mixed_condition_is_reported(project_mode, key_mode, cluster_setting, within):
    profile = {} if cluster_setting is None else {"automatic_execution_allowed": cluster_setting}
    result = approval_requirements({"mode": project_mode}, key_mode, {"site_profile": profile}, within_envelope=within)
    expected = []
    if project_mode == "approval_required":
        expected.append("PROJECT_REVIEW_REQUIRED")
    if key_mode == "approval_required":
        expected.append("KEY_REVIEW_REQUIRED")
    if cluster_setting is not True:
        expected.append("CLUSTER_REVIEW_REQUIRED")
    if project_mode == "mixed" and within is False:
        expected.append("PROJECT_ENVELOPE_REVIEW_REQUIRED")
    assert [reason["code"] for reason in result["approval_reasons"]] == expected
    assert result["requires_human_approval"] is bool(expected)
    assert bool(result["conditional_approval_reasons"]) is (project_mode == "mixed" and within is None)
    assert bool(result["conditional_approval_notice"]) is bool(result["conditional_approval_reasons"])
    assert all(set(reason) == {"code", "source", "message"} for reason in result["approval_reasons"])


@pytest.mark.parametrize("within", [0, 1, "false", [], {}])
def test_unknown_envelope_comparisons_do_not_look_like_a_boolean_decision(within):
    with pytest.raises(ValueError):
        approval_requirements({"mode": "mixed"}, "inherit", {}, within_envelope=within)


def test_explanations_do_not_copy_private_cluster_settings():
    result = approval_requirements({"mode": "budgeted"}, "inherit", {
        "site_profile": {"automatic_execution_allowed": False, "private": "PRIVATE-PROFILE"},
        "ssh": {"identity_file": "PRIVATE-KEY-PATH"},
    })
    assert "PRIVATE" not in str(result)
    assert result["approval_reasons"][0]["source"] == "cluster"
    assert "Cluster configuration" in result["approval_reasons"][0]["message"]
