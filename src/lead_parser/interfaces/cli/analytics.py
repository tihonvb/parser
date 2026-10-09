"""Print lead economics from a standalone SQLite backup without production access."""

import argparse
import json
import sqlite3

from lead_parser.application.analytics import Period, build_report, money, render_markdown
from lead_parser.infrastructure.persistence.analytics import read_snapshot, read_usage


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="Standalone SQLite backup (not the live database)")
    parser.add_argument("--from", dest="start", required=True, metavar="YYYY-MM-DD")
    parser.add_argument("--to", dest="end", required=True, metavar="YYYY-MM-DD", help="Exclusive end date")
    parser.add_argument(
        "--timezone", default="UTC", help="IANA timezone for midnight boundaries (default UTC)"
    )
    parser.add_argument("--usage-log", help="Usage JSONL copy; default DATABASE.usage.jsonl")
    parser.add_argument(
        "--ai-cost-usd", help="Actual project AI invoice for the period; replaces logged subtotal"
    )
    parser.add_argument(
        "--fixed-cost-usd", help="All other costs allocated to the period, in USD; explicit 0 allowed"
    )
    parser.add_argument("--format", choices=["json", "markdown"], default="markdown")
    parser.add_argument(
        "--include-leads", action="store_true", help="Include accepted lead URLs, AI reasons and human labels"
    )
    args = parser.parse_args(argv)
    try:
        period = Period.dates(args.start, args.end, args.timezone)
        ai_cost = money(args.ai_cost_usd) if args.ai_cost_usd is not None else None
        fixed_cost = money(args.fixed_cost_usd) if args.fixed_cost_usd is not None else None
        leads, deliveries = read_snapshot(args.database, period)
        events, available, invalid = read_usage(args.usage_log or args.database + ".usage.jsonl")
        report = build_report(
            leads,
            deliveries,
            events,
            period,
            usage_available=available,
            invalid_usage_lines=invalid,
            ai_cost_usd=ai_cost,
            fixed_cost_usd=fixed_cost,
            include_leads=args.include_leads,
        )
        output = (
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
            if args.format == "json"
            else render_markdown(report)
        )
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.error(str(error))
    print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
