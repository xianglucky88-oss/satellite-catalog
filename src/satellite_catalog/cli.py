import argparse
import json

from .application import (
    apply_declaration_file,
    query_available,
    query_explain,
    query_lineage,
    run_demo,
    run_fixture,
    run_url,
)


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description="Catalog ingestion and product version lineage")
    commands = parser.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser("ingest", help="run fixture or HTTP JSON ingestion")
    source = ingest.add_mutually_exclusive_group(required=True)
    source.add_argument("--fixture", help="local JSON list fixture")
    source.add_argument("--url", help="HTTP(S) JSON endpoint")
    ingest.add_argument("--database", required=True)
    ingest.add_argument("--timeout", type=float, default=30.0)
    ingest.add_argument("--raw-dir", help="directory for immutable raw HTTP responses")
    ingest.add_argument("--records-key", help="dot-separated JSON key containing the record list")

    declare = commands.add_parser("declare", help="apply one lineage declaration revision")
    declare.add_argument("--declaration", required=True, help="JSON declaration file")
    declare.add_argument("--database", required=True)

    available = commands.add_parser("available", help="list available terminal versions of a product")
    available.add_argument("--product-id", required=True)
    available.add_argument("--database", required=True)

    lineage = commands.add_parser("lineage", help="show supersession paths of one version")
    lineage.add_argument("--product-id", required=True)
    lineage.add_argument("--version", required=True)
    lineage.add_argument("--database", required=True)

    explain = commands.add_parser("explain", help="explain why a version is or is not available")
    explain.add_argument("--product-id", required=True)
    explain.add_argument("--version", required=True)
    explain.add_argument("--database", required=True)

    demo = commands.add_parser("demo", help="run the step-by-step lineage demo on a fresh database")
    demo.add_argument("--database", required=True)

    args = parser.parse_args()

    if args.command == "ingest":
        if args.fixture:
            _print(run_fixture(args.fixture, args.database))
        else:
            _print(run_url(
                args.url,
                args.database,
                timeout=args.timeout,
                raw_dir=args.raw_dir,
                records_key=args.records_key,
            ))
    elif args.command == "declare":
        _print(apply_declaration_file(args.declaration, args.database))
    elif args.command == "available":
        _print(query_available(args.database, args.product_id))
    elif args.command == "lineage":
        _print(query_lineage(args.database, args.product_id, args.version))
    elif args.command == "explain":
        _print(query_explain(args.database, args.product_id, args.version))
    elif args.command == "demo":
        for step in run_demo(args.database):
            print(f"== {step['step']}")
            for key, value in step.items():
                if key == "step":
                    continue
                print(f"   {key}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}")


if __name__ == "__main__":
    main()
