"""Run the small page with the list of leads and the Telegram button listener in one process."""

import argparse
import logging
import signal
import threading
import time
from http.server import ThreadingHTTPServer

import requests
from filelock import FileLock

from lead_parser.infrastructure.configuration import CONFIG_PATH, load_config, web_required
from lead_parser.infrastructure.integrations.telegram.claims import person_name, poll_once
from lead_parser.infrastructure.persistence.sqlite import Store
from lead_parser.interfaces.web.app import make_handler


def listen(cfg: dict, stop: threading.Event) -> None:
    failures = 0
    while not stop.is_set():
        try:
            with Store(cfg["storage"]["database"]) as store:
                poll_once(cfg, store)
            failures = 0
        except Exception as error:
            failures += 1
            logging.warning("button listener failed: %s", type(error).__name__)
            stop.wait(min(300, 5 * 2 ** min(failures, 6)))
        time.sleep(0.2)


def click_notifier(cfg: dict):
    """Tell the admins (private chats) that a recipient opened an order; first click only."""
    tg = cfg["notifications"]["telegram"]
    admins = [str(item).strip() for item in tg["click_notify_to"] if str(item).strip()]
    if not (tg["click_notify"] and admins):
        return None

    def notify(lead, chat_id: str) -> None:
        with Store(cfg["storage"]["database"]) as store:
            who = person_name(cfg, store, chat_id)
        title = " ".join(lead.text.split())[:90]
        for admin in admins:  # one failing chat (e.g. the bot was never started there) must not stop the rest
            try:
                requests.post(
                    f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage",
                    json={
                        "chat_id": admin,
                        "text": f"👁 {who} открыл(а) заказ\n{title}",
                        "disable_web_page_preview": True,
                    },
                    timeout=(5, 10),
                )
            except requests.RequestException:
                pass

    return notify


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument(
        "--check-enabled", action="store_true", help="systemd condition: no network or DB writes"
    )
    args = parser.parse_args(argv)
    cfg = load_config(args.config, require_access=False)
    if args.check_enabled:
        return 0 if web_required(cfg) else 1
    web = cfg["web"]
    handler = make_handler(
        cfg["storage"]["database"],
        web["token"],
        web["title"],
        cfg["notifications"]["telegram"]["names"],
        panel=web["panel"],
        on_first_click=click_notifier(cfg),
        name_of=lambda store, chat: person_name(cfg, store, chat),
    )
    with FileLock(cfg["storage"]["lock_file"] + ".web", timeout=0):
        # Ensure a current schema before accepting callbacks; never migrate implicitly.
        with Store(cfg["storage"]["database"]):
            pass
        server = ThreadingHTTPServer((web["host"], web["port"]), handler)
        stop = threading.Event()
        listener = None
        tg = cfg["notifications"]["telegram"]
        if tg["enabled"] and tg["claim_buttons"]:
            listener = threading.Thread(target=listen, args=(cfg, stop), daemon=True)
            listener.start()

        def shutdown(_signum, _frame):
            stop.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

        previous = {sig: signal.signal(sig, shutdown) for sig in (signal.SIGTERM, signal.SIGINT)}
        logging.basicConfig(level=logging.INFO)
        try:
            server.serve_forever()
        finally:
            stop.set()
            server.server_close()
            if listener:
                listener.join(timeout=40)
            for sig, original in previous.items():
                signal.signal(sig, original)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
