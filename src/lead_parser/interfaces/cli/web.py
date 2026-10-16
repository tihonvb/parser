"""Run the small page with the list of leads and the Telegram button listener in one process."""

import argparse
import logging
import threading
import time
from http.server import ThreadingHTTPServer

from lead_parser.infrastructure.configuration import CONFIG_PATH, load_config
from lead_parser.infrastructure.integrations.telegram.claims import poll_once
from lead_parser.infrastructure.persistence.sqlite import Store
from lead_parser.interfaces.web.app import make_handler


def listen(cfg: dict, stop: threading.Event) -> None:
    failures = 0
    while not stop.is_set():
        try:
            with Store(cfg["storage"]["database"]) as store:
                poll_once(cfg, store)
            failures = 0
        except Exception:
            failures += 1
            logging.exception("button listener failed")
            stop.wait(min(300, 5 * 2 ** min(failures, 6)))
        time.sleep(0.2)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(CONFIG_PATH))
    args = parser.parse_args(argv)
    cfg = load_config(args.config, require_access=False)
    web = cfg["web"]
    handler = make_handler(
        cfg["storage"]["database"], web["token"], web["title"], cfg["notifications"]["telegram"]["names"]
    )
    stop = threading.Event()
    tg = cfg["notifications"]["telegram"]
    if tg["enabled"] and tg["claim_buttons"]:
        threading.Thread(target=listen, args=(cfg, stop), daemon=True).start()
    server = ThreadingHTTPServer((web["host"], web["port"]), handler)
    logging.basicConfig(level=logging.INFO)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
