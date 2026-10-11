"""Browser session for the specialist cabinet: an already authorised context, closed exactly as owned.

Two ways to get an authorised session (docs/profi-connector.md):

* ``profi.storage_state`` — a Playwright session file produced by ``lead-parser profi-auth login`` on a
  machine with a display, then uploaded to the server (mode 0600);
* ``profi.cdp_endpoint`` — attach to a browser that is already logged in. Its existing context and tabs are
  never closed; only the page this run opened.

The connector never types a password or an SMS code and never solves a captcha.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from lead_parser.infrastructure.integrations.profi.pages import ProfiError
from lead_parser.infrastructure.security import safe_error


class ProfiBrowser:
    def __init__(self, pw, settings: dict):
        self.pw, self.settings = pw, settings
        self.browser = self.context = None
        self.owns_browser = self.owns_context = False
        self.pages: list = []
        self.cleanup_errors: list[str] = []

    @property
    def state_path(self) -> Path:
        return Path(self.settings["storage_state"])

    def open(self) -> ProfiBrowser:
        timeout = self.settings["request_timeout_seconds"] * 1000
        endpoint = self.settings["cdp_endpoint"].strip()
        if endpoint:
            self.browser = self.pw.chromium.connect_over_cdp(endpoint, timeout=timeout)
            if self.browser.contexts:
                self.context = self.browser.contexts[0]  # the user's own logged-in context: never closed
            else:
                self.context = self.browser.new_context()
                self.owns_context = True
            return self
        if not self.state_path.is_file():
            raise ProfiError("AuthRequired:session file not found; run `lead-parser profi-auth login`")
        self.browser = self.pw.chromium.launch(headless=self.settings["headless"])
        self.owns_browser = True
        self.context = self.browser.new_context(storage_state=str(self.state_path), locale="ru-RU")
        self.owns_context = True
        return self

    def page(self):
        page = self.context.new_page()
        page.set_default_timeout(self.settings["request_timeout_seconds"] * 1000)
        self.pages.append(page)
        return page

    def close_page(self, page) -> None:
        """Close one page right after use so a stuck page cannot affect the next address."""
        try:
            page.close()
        except Exception as error:
            self.cleanup_errors.append(f"Cleanup:page:{safe_error(error)}")
        if page in self.pages:
            self.pages.remove(page)

    def save_session(self) -> None:
        """Persist refreshed cookies of a context we own (atomically, owner-only)."""
        if not (self.owns_context and self.owns_browser and self.context):
            return
        state = self.context.storage_state()
        write_state(self.state_path, state)

    def close(self) -> list[str]:
        errors = list(self.cleanup_errors)
        actions = [("page", page.close) for page in self.pages]
        if self.owns_context and self.context:
            actions.append(("context", self.context.close))
        if self.browser and (self.owns_browser or self.settings["cdp_endpoint"].strip()):
            actions.append(("browser", self.browser.close))  # CDP: only detaches Playwright
        for name, action in actions:
            try:
                action()
            except Exception as error:
                errors.append(f"Cleanup:{name}:{safe_error(error)}")
        return errors


def write_state(path: Path, state: dict) -> None:
    """Write a session file atomically with owner-only permissions from the first byte."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(state, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    if os.name == "posix":
        path.chmod(0o600)
