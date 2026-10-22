"""Public Hermes configuration boundary; task IDs are not filesystem authority."""

import os

from .engine import InspectionError


def local_file_context():
    """Return the confirmed local namespace without guessing a task's cwd."""
    try:
        from hermes_cli.config import load_config_readonly
        from hermes_constants import get_subprocess_home

        terminal = load_config_readonly().get("terminal")
        if not isinstance(terminal, dict):
            raise InspectionError("file_context_unavailable")
        backend = terminal.get("backend")
        if not isinstance(backend, str) or not backend.strip():
            raise InspectionError("file_context_unavailable")
        carrier = os.environ.get("TERMINAL_ENV", "").strip().lower()
        local = backend.strip().lower() == "local" and carrier in ("", "local")
        return {"local": local, "base": None, "home": get_subprocess_home() if local else None}
    except Exception as exc:
        raise InspectionError("file_context_unavailable") from exc
