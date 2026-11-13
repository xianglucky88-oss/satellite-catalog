import argparse
import json

from .application import query_combination, run_fixture, run_url


def _add_ingest_parser(parser: argparse.ArgumentParser) -> None:
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--fixture", help="local JSON list fixture")
    source.add_argument("--url", help="HTTP(S) JSON endpoint")
    parser.add_argument("--database", required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--raw-dir", help="directory for immutable raw HTTP responses")
    parser.add_argument("--records-key", help="dot-separated JSON key containing the record list")


def _add_query_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database", required=True)
    parser.add_argument(
        "--target-bbox",
        required=True,
        help="target rectangle as west,south,east,north (degrees); east<west crosses the antimeridian",
    )
    parser.add_argument("--acquired-after", required=True, help="YYYY-MM-DD or ISO-8601 datetime")
    parser.add_argument("--acquired-before", required=True, help="YYYY-MM-DD or ISO-8601 datetime")
    parser.add_argument("--max-cloud-cover", required=True, type=float)
    parser.add_argument("--budget", required=True, type=int, help="maximum number of products")
    parser.add_argument("--max-candidates", type=int, default=None)
    parser.add_argument("--max-combinations", type=int, default=None)


def _run_ingest(args: argparse.Namespace) -> dict:
    if args.fixture:
        return run_fixture(args.fixture, args.database)
    return run_url(
        args.url,
        args.database,
        timeout=args.timeout,
        raw_dir=args.raw_dir,
        records_key=args.records_key,
    )


def _run_query(args: argparse.Namespace) -> dict:
    return query_combination(
        args.database,
        target_bbox=args.target_bbox,
        acquired_after=args.acquired_after,
        acquired_before=args.acquired_before,
        max_cloud_cover=args.max_cloud_cover,
        budget=args.budget,
        max_candidates=args.max_candidates,
        max_combinations=args.max_combinations,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Satellite catalog ingestion and combination query")
    subparsers = parser.add_subparsers(dest="command")

    ingest = subparsers.add_parser("ingest", help="ingest fixture or HTTP JSON records")
    _add_ingest_parser(ingest)
    ingest.set_defaults(handler=_run_ingest)

    query = subparsers.add_parser("query", help="select a budgeted product combination for a target")
    _add_query_parser(query)
    query.set_defaults(handler=_run_query)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return
    result = args.handler(args)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
