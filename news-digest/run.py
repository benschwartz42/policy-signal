#!/usr/bin/env python3
"""Policy Signal — CLI entrypoint.

  python run.py --self-test                          offline, no keys, asserts behavior
  python run.py --config config.yaml --dry-run       live sources, writes artifacts, no email
  python run.py --config config.yaml                 live run + email + seen-store update
  python run.py --config config.yaml --emit-json out/latest.json   also write the companion JSON
"""

from __future__ import annotations

import argparse
import logging
import sys


def _load_dotenv() -> None:
    """Best-effort .env loading so local runs pick up keys without extra tooling."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="policy-signal", description="Policy Signal daily digest")
    parser.add_argument("--config", help="path to config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="run live sources but do not send email or update seen-store")
    parser.add_argument("--self-test", action="store_true", help="offline self-test, no keys or network")
    parser.add_argument("--emit-json", metavar="PATH", help="also write the structured digest JSON (for the companion app)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.self_test:
        from digest.pipeline import self_test
        try:
            self_test()
        except AssertionError as exc:
            print(f"SELF-TEST FAILED: {exc}", file=sys.stderr)
            return 1
        return 0

    if not args.config:
        parser.error("--config is required unless --self-test is given")

    _load_dotenv()
    from digest.config import load_config, ConfigError
    from digest.pipeline import run

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2

    result = run(config, dry_run=args.dry_run, emit_json_path=args.emit_json)
    where = "dry-run (no email)" if args.dry_run else f"delivered via {result.delivered_via}"
    print(f"Done: {result.payload['item_count']} items across "
          f"{result.payload['topic_count']} topics — {where}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
