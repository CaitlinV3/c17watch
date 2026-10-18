"""Command-line entry point: python -m c17watch --config config.toml"""

from __future__ import annotations

import argparse
import logging
import sys

import requests

from . import __version__
from .config import ConfigError, load_config
from .notify import Message, build_notifier
from .providers import AuthError, ProviderError, build_provider
from .ratelimit import BudgetExhausted
from .watcher import Watcher


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="c17watch", description=__doc__)
    ap.add_argument("-c", "--config", default="config.toml", help="path to config file")
    ap.add_argument("--once", action="store_true", help="poll once and exit (good for cron)")
    ap.add_argument("--dry-run", action="store_true", help="print alerts instead of sending them")
    ap.add_argument("--test-notify", action="store_true", help="send a test notification and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=f"c17watch {__version__}")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("c17watch")

    try:
        cfg = load_config(args.config)
        session = requests.Session()
        notifier = build_notifier(cfg.notify, session, args.dry_run)
        if args.test_notify:
            ok = notifier.send_with_retry(Message("c17watch test", "If you can read this, alerts work."))
            print("sent" if ok else "failed")
            return 0 if ok else 1
        provider = build_provider(cfg.provider)
    except (ConfigError, ValueError, AuthError) as e:
        log.error("%s", e)
        return 2

    watcher = Watcher(cfg, provider, notifier, session)
    if args.once:
        try:
            watcher.poll_once()
        except BudgetExhausted as e:
            log.warning("%s", e)
        except (AuthError, ProviderError) as e:
            log.error("%s", e)
            return 1
        return 0

    watcher.install_signal_handlers()
    watcher.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
