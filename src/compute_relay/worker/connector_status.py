"""Small, deliberately redacted terminal status for personal connectors.

Callers supply fixed local messages. Never pass remote output, exception text,
credentials, connection dictionaries or script contents to these methods.
"""

import sys


def display_label(value, *, fallback="cluster"):
    """Bound untrusted display names to one printable terminal line."""
    if not isinstance(value, str):
        return fallback
    clean = " ".join("".join(character if character.isprintable() else " " for character in value).split())
    return clean[:96] or fallback


def connector_label(state):
    ssh = state.get("connection", {}).get("ssh", {})
    fallback = display_label(ssh.get("host"))
    return display_label(state.get("name"), fallback=fallback)


class ConnectorStatus:
    def __init__(self, state, *, verbose=False, label=None):
        self.label = display_label(label, fallback=connector_label(state))
        self.verbose = verbose
        self._interruption = None

    def notice(self, message):
        print(f"[{self.label}] {message}", flush=True)

    def progress(self, message):
        if self.verbose:
            self.notice(message)

    def problem(self, message):
        print(f"[{self.label}] {message}", file=sys.stderr, flush=True)

    def interrupted(self, kind):
        if kind != self._interruption:
            if kind == "broker":
                self.problem("Broker communication interrupted; retrying. Recorded job state is retained.")
            else:
                self.problem("Operation interrupted; retained submission state will be reconciled.")
        self._interruption = kind

    def recovered(self):
        if self._interruption == "broker":
            self.notice("Broker communication restored; continuing to handle commands.")
        elif self._interruption:
            self.notice("Connector operation resumed; continuing to handle commands.")
        self._interruption = None
