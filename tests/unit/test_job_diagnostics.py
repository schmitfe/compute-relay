"""Bounded human summaries classify observed facts without copying private state."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.template import Context, Template
from django.template.loader import render_to_string

from compute_relay.control.job_diagnostics import MAX_LOG_BYTES, build_attempt_diagnostics


def attempt(**changes):
    return SimpleNamespace(id=uuid4(), job_id=uuid4(), connection_id=uuid4(),
        connection=SimpleNamespace(name="Synthetic cluster", configuration={"ssh": "PRIVATE-CONFIG-MUST-NOT-APPEAR"}),
        state="failed", accounting_state="settled", dispatch_started=True,
        native_identity={"job_id": "196068", "work_dir": "PRIVATE-WORK-DIRECTORY", "private": "PRIVATE-NATIVE-STATE"},
        observation={"native_state": "FAILED", "exit_code": "1:0", "source": "sacct", "allocated_cpus": 2,
            "allocated_gpus": 0, "elapsed_seconds": 1, "detail": "PRIVATE-RAW-DETAIL",
            "accounting": {"final": True, "cpu_core_seconds": 2, "gpu_device_seconds": 0}},
        error_code="", stdout="", stderr="", **changes)


def changed(**changes):
    value = attempt()
    for key, item in changes.items():
        setattr(value, key, item)
    return value


def rendered(value):
    return render_to_string("broker/attempt_diagnostics.html", {"diagnostics": build_attempt_diagnostics(value)})


def test_reported_failure_shows_typed_scheduler_facts_and_actual_usage_without_private_state():
    detail = build_attempt_diagnostics(attempt())
    assert detail["category"] == "failed" and detail["native_job_id"] == "196068"
    assert detail["exit_code"] == "1:0" and detail["source"] == "Slurm accounting"
    assert detail["allocated_cpus"] == 2 and detail["allocated_gpus"] == 0
    assert detail["cpu_core_seconds"] == 2 and detail["gpu_device_seconds"] == 0
    assert detail["accounting_state"] == "settled"
    assert "failed allocation can still consume" in detail["notices"][0]
    text = str(detail) + rendered(attempt())
    assert "PRIVATE-" not in text


@pytest.mark.parametrize("unresolved", ["execution", "accounting", "collection"])
def test_removed_cluster_exposes_last_recorded_status_without_inventing_final_usage(unresolved):
    value = changed()
    value.connection.archived = True
    if unresolved == "execution":
        value.state, value.accounting_state = "running", "reserved"
    elif unresolved == "accounting":
        value.accounting_state = "settlement_pending"
    else:
        value.collection_pending, value.collection_state = True, "retry_pending"
    detail = build_attempt_diagnostics(value)
    assert detail["category"] == "cluster_removed_unresolved" and detail["connection_removed"] is True
    text = rendered(value)
    assert "last recorded status" in text and "configuration removed" in text
    assert '/clusters/' not in text and "retry automatically" not in text
    assert detail["state"] == value.state and detail["accounting_state"] == value.accounting_state


def test_removed_cluster_keeps_confirmed_finished_diagnostics():
    value = changed()
    value.connection.archived = True
    detail = build_attempt_diagnostics(value)
    assert detail["category"] == "failed" and detail["accounting_state"] == "settled"
    assert detail["cpu_core_seconds"] == 2 and detail["connection_removed"] is True


@pytest.mark.parametrize("path", ["/usr/share/lmod/lmod/init/bash", "/usr/share/lmod/8.7.47/init/bash",
    "/etc/profile.d/modules.sh", "/usr/share/Modules/init/bash"])
def test_missing_known_initializer_reports_environment_cause_without_claiming_python_ran(path):
    value = changed(stderr=f"./_broker/environment.sh: line 5: {path}: No such file or directory\n")
    detail = build_attempt_diagnostics(value)
    assert detail["category"] == "module_initializer_missing"
    assert detail["title"] == "Module initialization file is missing"
    assert "not evidence that the Python script ran" in detail["message"]
    assert "cluster-provided module command" in detail["suggestion"]
    assert "new job for review" in detail["suggestion"]
    assert "saved failed package stays unchanged" in detail["suggestion"]
    assert path in detail["stderr"]


@pytest.mark.parametrize("marker, category", [("MODULE_INITIALIZER_MISSING", "module_initializer_missing"),
    ("MODULE_COMMAND_UNAVAILABLE", "module_command_unavailable")])
def test_fixed_environment_markers_have_explicit_next_steps(marker, category):
    detail = build_attempt_diagnostics(changed(stderr="BROKER_ENVIRONMENT_ERROR:" + marker + "\n"))
    assert detail["category"] == category
    assert detail["message"] and detail["suggestion"]


@pytest.mark.parametrize("marker, category", [("MODULE_INITIALIZER_UNREADABLE: /private/init", "module_initializer_unreadable"),
    ("MODULE_COMMAND_UNAVAILABLE: module command was not provided", "module_command_unavailable")])
def test_new_wrapper_markers_with_space_and_details_are_classified_without_overclaim(marker, category):
    detail = build_attempt_diagnostics(changed(stderr="BROKER_ENVIRONMENT_ERROR: " + marker + "\n"))
    assert detail["category"] == category
    assert "/private/init" not in detail["message"] + detail["suggestion"]
    if category == "module_initializer_unreadable":
        assert "may be missing or inaccessible" in detail["message"]


@pytest.mark.parametrize("stderr", ["python3: ./data.csv: No such file or directory",
    "./_broker/environment.sh: line 5: /private/data: No such file or directory",
    "arbitrary module command not found text", "The text BROKER_ENVIRONMENT_ERROR:MODULE_INITIALIZER_MISSING is quoted here."])
def test_generic_missing_files_or_quoted_marker_do_not_invent_module_failure(stderr):
    assert build_attempt_diagnostics(changed(stderr=stderr))["category"] == "failed"


@pytest.mark.parametrize("native_state, category", [("OUT_OF_MEMORY", "out_of_memory"), ("TIMEOUT", "timeout"),
    ("NODE_FAIL", "node_failure"), ("BOOT_FAIL", "node_failure"), ("CANCELLED by 1234", "cancelled"),
    ("PREEMPTED", "preempted"), ("DEADLINE", "scheduler_failure"), ("REVOKED", "scheduler_failure")])
def test_scheduler_reported_states_have_bounded_fixed_explanations(native_state, category):
    value = attempt()
    value.observation["native_state"] = native_state
    detail = build_attempt_diagnostics(value)
    assert detail["category"] == category
    assert detail["title"] and detail["message"] and detail["suggestion"]
    assert "1234" not in detail.get("native_state", "")


def test_signal_or_generic_failure_text_alone_is_not_guessed_as_oom():
    value = changed(stderr="Killed\npossible memory problem\n")
    value.observation["exit_code"] = "0:9"
    assert build_attempt_diagnostics(value)["category"] == "failed"


def test_uncertain_submission_overrides_old_failure_logs_and_never_advises_retry():
    value = changed(state="submission_uncertain", accounting_state="reserved",
        stderr="BROKER_ENVIRONMENT_ERROR:MODULE_INITIALIZER_MISSING\n")
    detail = build_attempt_diagnostics(value)
    assert detail["category"] == "submission_uncertain"
    assert "do not submit a replacement" in detail["suggestion"]
    assert "reservations can remain held" in detail["notices"][0]


def test_definitive_rejection_requires_recorded_release_dispatch_and_no_native_identity():
    value = changed(accounting_state="released", native_identity=None, observation={})
    assert build_attempt_diagnostics(value)["category"] == "rejected"
    value.accounting_state = "reserved"
    assert build_attempt_diagnostics(value)["category"] == "failed"
    value.accounting_state, value.dispatch_started = "released", False
    assert build_attempt_diagnostics(value)["category"] == "failed"
    value.dispatch_started, value.native_identity = True, {"job_id": "1"}
    assert build_attempt_diagnostics(value)["category"] == "failed"


def test_cancel_request_is_not_a_confirmed_cancellation_or_refund():
    detail = build_attempt_diagnostics(changed(state="cancel_requested", accounting_state="reserved"))
    assert detail["category"] == "cancel_pending"
    assert "not recorded yet" in detail["message"]
    assert "not treat this request as a refund" in detail["suggestion"]


@pytest.mark.parametrize("error", ["OUTPUT_COLLECTION_FAILED", "OUTPUT_STORAGE_UNAVAILABLE", "OUTPUT_MISSING"])
def test_missing_outputs_do_not_reclassify_success_as_program_failure_or_hide_accounting(error):
    value = changed(state="succeeded", error_code=error)
    value.observation["native_state"] = "COMPLETED"
    detail = build_attempt_diagnostics(value)
    assert detail["category"] == "outputs_unavailable"
    assert "does not prove that the program failed" in detail["message"]
    assert detail["accounting_state"] == "settled" and detail["cpu_core_seconds"] == 2


def test_output_failure_is_secondary_when_native_allocation_failed():
    detail = build_attempt_diagnostics(changed(error_code="OUTPUT_COLLECTION_FAILED"))
    assert detail["category"] == "failed"
    assert any("separate from the allocation result" in message for message in detail["notices"])


@pytest.mark.parametrize("error,category,phrase", [
    ("CANCEL_UNCONFIRMED", "cancel_unconfirmed", "retains cancellation intent"),
    ("CANCEL_IDENTITY_MISMATCH", "cancel_identity_mismatch", "refused to cancel"),
])
def test_unconfirmed_cancellation_has_fixed_actionable_diagnostics(error, category, phrase):
    value = changed(state="cancel_pending", accounting_state="reserved", error_code=error,
        observation={"detail": "PRIVATE-SSH-DIAGNOSTIC"})
    detail = build_attempt_diagnostics(value)
    assert detail["category"] == category and phrase in detail["message"]
    assert detail["accounting_state"] == "reserved"
    assert "PRIVATE-" not in rendered(value)


def test_transport_failure_uses_public_code_not_raw_observation_or_exception():
    value = changed(state="queued", error_code="AUTHENTICATION_UNAVAILABLE", observation={"detail": "PRIVATE-SSH-ERROR", "exception": "PRIVATE-EXCEPTION"})
    detail = build_attempt_diagnostics(value)
    assert detail["category"] == "connector_unavailable"
    assert "Private SSH diagnostics are not included" in detail["message"]
    assert "PRIVATE-" not in rendered(value)


def test_overage_notice_does_not_erase_recorded_usage_or_invent_scheduler_failure():
    value = changed(state="succeeded", error_code="OBSERVED_OVERAGE")
    value.observation["native_state"] = "COMPLETED"
    detail = build_attempt_diagnostics(value)
    assert detail["category"] == "succeeded"
    assert any("debt" in notice for notice in detail["notices"])


@pytest.mark.parametrize("state, category", [("running", "in_progress"), ("authorized", "in_progress"),
    ("expired", "expired"), ("unknown", "unknown")])
def test_nonfailure_and_unknown_states_are_not_presented_as_python_errors(state, category):
    detail = build_attempt_diagnostics(changed(state=state, observation={}))
    assert detail["category"] == category


def test_template_escapes_logs_metadata_and_enforces_autoescape_even_when_parent_disables_it():
    value = changed(stdout="<script>alert('x')</script>", stderr='<img src=x onerror="danger()">')
    value.connection.name = '<a href="evil">cluster</a>'
    context = {"diagnostics": build_attempt_diagnostics(value)}
    text = Template('{% autoescape off %}{% include "broker/attempt_diagnostics.html" %}{% endautoescape %}').render(Context(context))
    assert "<script>" not in text and "<img src=x" not in text
    assert "&lt;script&gt;" in text and "&lt;img" in text
    assert "&lt;a href=" in text
    assert "196068" in text and "1:0" in text and "2 CPU-core seconds" in text
    assert "0 GPU-device seconds" in text
    assert 'data-state="failed"' in text and 'data-accounting-state="settled"' in text


def test_both_log_previews_are_byte_bounded_and_report_truncation_without_broken_utf8():
    value = changed(stdout="é" * MAX_LOG_BYTES, stderr="x" * (MAX_LOG_BYTES * 3))
    detail = build_attempt_diagnostics(value)
    assert len(detail["stdout"].encode()) <= MAX_LOG_BYTES
    assert len(detail["stderr"].encode()) <= MAX_LOG_BYTES
    assert detail["stdout_truncated"] and detail["stderr_truncated"]
    assert "�" not in detail["stdout"]
    assert "preview is limited" in rendered(value)


def test_patterns_after_the_bounded_preview_are_not_claimed_observed_in_summary():
    value = changed(stderr="x" * (MAX_LOG_BYTES + 1) + "\nBROKER_ENVIRONMENT_ERROR:MODULE_INITIALIZER_MISSING\n")
    detail = build_attempt_diagnostics(value)
    assert detail["category"] == "failed" and detail["stderr_truncated"]


def test_malformed_typed_fields_are_not_stringified_or_copied():
    value = changed(state={"PRIVATE-STATE": 1}, error_code="secret/path\npassword", stdout={"secret": 1}, stderr=None,
        native_identity={"job_id": "PRIVATE-JOB", "other": "PRIVATE-NATIVE"},
        observation={"native_state": "PRIVATE-STATE", "source": ["PRIVATE-SOURCE"], "exit_code": "PRIVATE-EXIT",
            "allocated_cpus": True, "allocated_gpus": -1, "elapsed_seconds": float("nan"),
            "accounting": {"final": "true", "cpu_core_seconds": 2**53, "gpu_device_seconds": "PRIVATE-USAGE"}})
    detail = build_attempt_diagnostics(value)
    assert detail["native_job_id"] is detail["exit_code"] is detail["native_state"] is None
    assert detail["allocated_cpus"] is detail["allocated_gpus"] is detail["elapsed_seconds"] is None
    assert detail["cpu_core_seconds"] is detail["gpu_device_seconds"] is detail["accounting_final"] is None
    assert "PRIVATE-" not in str(detail)
    assert detail["stdout"] == detail["stderr"] == ""
