"""profi.ru specialist session tools: log in once, check the session, calibrate selectors.

    lead-parser profi-auth login   [--output FILE]   # headed browser; you log in yourself (phone + SMS);
                                                     # run on a machine WITH A DISPLAY, then upload FILE
    lead-parser profi-auth status                    # opens the first profi.urls page with the saved session
    lead-parser profi-auth inspect [--url URL] [--out FILE]
                                                     # saves the page STRUCTURE with all texts, numbers and
                                                     # addresses masked (letters -> x, digits -> 9)

The tool never stores or types a password or an SMS code: authorisation is performed by you in a normal
browser window; only the resulting session file is saved (mode 0600). See docs/profi-connector.md.
"""

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from lead_parser.infrastructure.configuration import CONFIG_PATH, load_config
from lead_parser.infrastructure.integrations.profi.browser import ProfiBrowser, write_state
from lead_parser.infrastructure.integrations.profi.pages import ProfiError, check_page, mask_html
from lead_parser.infrastructure.security import safe_error

START_URL = "https://profi.ru/"
EXIT_OK, EXIT_NOT_READY = 0, 2


def login(cfg: dict, output: Path, playwright_factory=sync_playwright, wait=input) -> int:
    with playwright_factory() as pw:
        browser = pw.chromium.launch(headless=False)
        try:
            context = browser.new_context(locale="ru-RU")
            page = context.new_page()
            page.goto(START_URL)
            print("A browser window is open. Log in to your SPECIALIST account yourself, then open the page")
            print("with orders so you are sure you are inside the cabinet.")
            wait("Press Enter here when you are logged in... ")
            write_state(output, context.storage_state())
            print(
                f"Session saved: {output} (owner-only). Upload it to the server's profi.storage_state path."
            )
        finally:
            browser.close()
    return EXIT_OK


def _open_first(cfg: dict, url: str | None, playwright_factory, action):
    settings = cfg["profi"]
    target = url or (settings["urls"][0] if settings["urls"] else "")
    if not target:
        print("No page to open: set profi.urls or pass --url.")
        return EXIT_NOT_READY
    with playwright_factory() as pw:
        browser = ProfiBrowser(pw, settings)
        try:
            browser.open()
            page = browser.page()
            response = page.goto(
                target, wait_until="domcontentloaded", timeout=settings["request_timeout_seconds"] * 1000
            )
            return action(page, response, settings)
        except ProfiError as error:
            print(f"Not ready: {error}")
            return EXIT_NOT_READY
        except Exception as error:
            print(f"Not ready: {safe_error(error)}")
            return EXIT_NOT_READY
        finally:
            for problem in browser.close():
                print(problem)


def status(cfg: dict, url: str | None = None, playwright_factory=sync_playwright) -> int:
    def action(page, response, settings):
        selectors = settings["selectors"]
        check_page(page, response, selectors)
        marker = selectors.get("logged_in", "").strip()
        if marker and not page.query_selector(marker):
            print("Not ready: the cabinet marker is missing; the session may have expired.")
            return EXIT_NOT_READY
        card = selectors.get("card", "").strip()
        count = len(page.query_selector_all(card)) if card else 0
        print(
            f"Session OK. Order cards found on the page: {count}"
            + ("" if card else " (profi.selectors.card is not set)")
        )
        return EXIT_OK

    return _open_first(cfg, url, playwright_factory, action)


def inspect(cfg: dict, url: str | None, out: Path, playwright_factory=sync_playwright) -> int:
    def action(page, response, settings):
        check_page(page, response, settings["selectors"])
        page.wait_for_timeout(3000)
        skeleton = mask_html(page.content())
        out.write_text(skeleton, encoding="utf-8")
        print(
            f"Masked page structure saved: {out} ({len(skeleton)} characters). Texts, numbers and addresses"
        )
        print("are reduced to their shape (x for letters, 9 for digits); class names and tags are kept.")
        return EXIT_OK

    return _open_first(cfg, url, playwright_factory, action)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["login", "status", "inspect"])
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument(
        "--output", help="login: where to save the session file (default: profi.storage_state)"
    )
    parser.add_argument("--url", help="status/inspect: page to open (default: the first profi.urls entry)")
    parser.add_argument("--out", default="profi-page-skeleton.html", help="inspect: file for the masked page")
    args = parser.parse_args(argv)
    cfg = load_config(args.config, require_access=False)
    if args.command == "login":
        return login(cfg, Path(args.output or cfg["profi"]["storage_state"]))
    if args.command == "status":
        return status(cfg, args.url)
    return inspect(cfg, args.url, Path(args.out))


if __name__ == "__main__":
    sys.exit(main())
