import argparse
import json

from .application import run_fixture, run_url


def main() -> None:
    parser = argparse.ArgumentParser(description="Run fixture or HTTP JSON ingestion")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--fixture", help="local JSON list fixture")
    source.add_argument("--url", help="HTTP(S) JSON endpoint")
    parser.add_argument("--database", required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--raw-dir", help="directory for immutable raw HTTP responses")
    parser.add_argument("--records-key", help="dot-separated JSON key containing the record list")
    parser.add_argument(
        "--access-scope",
        help="explicit cache scope for this view of the endpoint; "
        "cached records are never reused across scopes",
    )
    args = parser.parse_args()
    if args.fixture:
        result = run_fixture(args.fixture, args.database)
    else:
        result = run_url(
            args.url,
            args.database,
            timeout=args.timeout,
            raw_dir=args.raw_dir,
            records_key=args.records_key,
            access_scope=args.access_scope,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
