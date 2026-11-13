import json
import subprocess
import sys
from pathlib import Path

from satellite_catalog.application import run_fixture


def test_cli_query_subcommand_emits_json_result(tmp_path):
    db = str(tmp_path / "cli.sqlite3")
    fixture = str(Path(__file__).parents[1] / "fixtures" / "catalog_query.json")
    run_fixture(fixture, db)
    repo_root = Path(__file__).parents[1]
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "satellite_catalog.cli",
            "query",
            "--database",
            db,
            "--target-bbox",
            "0,0,10,10",
            "--acquired-after",
            "2026-10-01",
            "--acquired-before",
            "2026-10-31",
            "--max-cloud-cover",
            "10",
            "--budget",
            "2",
        ],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(repo_root / "src"), "PATH": "/usr/bin:/bin"},
        check=True,
    )
    result = json.loads(completed.stdout)
    assert [item["product_id"] for item in result["selected"]] == ["P-A", "P-B"]
    assert result["optimal"] is True
