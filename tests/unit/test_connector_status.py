"""Only bounded printable names and fixed connector messages reach terminals."""

from compute_broker.worker.connector_status import ConnectorStatus, connector_label, display_label


def test_label_removes_terminal_control_characters_and_is_bounded():
    label = display_label("Research\n\x1b[31m\u202e" + "x" * 200)
    assert len(label) == 96 and all(character.isprintable() for character in label)
    assert "\x1b" not in label and "\n" not in label and "\u202e" not in label


def test_existing_state_uses_host_name_without_printing_secret_fields():
    state = {"connection": {"ssh": {"host": "lab.example", "identity_file": "/private/key"}}, "worker_token": "secret"}
    assert connector_label(state) == "lab.example"


def test_progress_is_opt_in_and_interruptions_are_deduplicated(capsys):
    status = ConnectorStatus({"name": "lab"})
    status.progress("This is verbose only.")
    status.interrupted("operation")
    status.interrupted("operation")
    status.recovered()
    status.recovered()
    output = capsys.readouterr()
    assert "verbose only" not in output.out
    assert output.err.count("Operation interrupted") == 1
    assert output.out.count("operation resumed") == 1
