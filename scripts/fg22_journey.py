"""Run the non-browser half of the FG-22 journey against real services."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
PYTHON = Path(sys.executable)


def run_mode(mode: str, shop_file: Path, output_dir: Path) -> tuple[int, dict[str, Any]]:
    output = output_dir / f"{mode}.json"
    command = [
        str(PYTHON),
        "-m",
        "tests.e2e.flagship_e2e_fixture",
        mode,
        "--shop-file",
        str(shop_file),
        "--out",
        str(output),
    ]
    completed = subprocess.run(
        command,
        cwd=BACKEND,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "FLAGSHIP_ALLOW_WRITES": "1"},
        check=False,
        timeout=600,
    )
    payload: dict[str, Any] = {}
    if output.is_file():
        payload = json.loads(output.read_text(encoding="utf-8"))
    return completed.returncode, payload


def main() -> int:
    checklist: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="fg22-journey-") as temporary:
        directory = Path(temporary)
        seed_output = directory / "seed.json"
        seed = subprocess.run(
            [str(PYTHON), "-m", "tests.e2e.flagship_e2e_fixture", "seed", "--out", str(seed_output)],
            cwd=BACKEND,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "FLAGSHIP_ALLOW_WRITES": "1"},
            check=False,
            timeout=600,
        )
        if seed.returncode != 0 or not seed_output.is_file():
            print(json.dumps({"checklist": [], "error": "seed failed", "exit_code": seed.returncode}))
            return 1
        shop_file = directory / "shop.json"
        shop_file.write_text(seed_output.read_text(encoding="utf-8"), encoding="utf-8")
        marker = json.loads(shop_file.read_text(encoding="utf-8")).get("marker")

        modes = ("http", "service", "interrupt", "decision", "resume", "verify")
        reports: dict[str, dict[str, Any]] = {}
        try:
            for mode in modes:
                exit_code, report = run_mode(mode, shop_file, directory)
                reports[mode] = report
                checklist.append({
                    "name": f"{mode} fixture completed against real database",
                    "expected": 0,
                    "actual": exit_code,
                    "pass": exit_code == 0,
                })

            http_report = reports.get("http", {})
            observations = http_report.get("observations", {})
            staff_permissions = observations.get("staff_permissions", [])
            tool_registry = http_report.get("steps", {}).get("agent_admin_tools", {})
            checklist.append({
                "name": "real HTTP agent and governance observations completed",
                "expected": True,
                "actual": bool(staff_permissions) and tool_registry.get("status") == 200,
                "pass": bool(staff_permissions) and tool_registry.get("status") == 200,
            })
            verify = reports.get("verify", {})
            facts = verify.get("verified", [])
            required = ["Order", "Payment", "PaymentCallback", "InventoryMovement", "Fulfillment", "Audit", "Outbox"]
            checklist.append({
                "name": "seven database fact classes were read from MySQL",
                "expected": required,
                "actual": facts,
                "pass": facts == required,
            })
        finally:
            cleanup_code, cleanup = run_mode("cleanup", shop_file, directory)
            checklist.append({
                "name": "fixture cleanup completed with no merchant residue",
                "expected": {"pending_actions": 0, "agent_runs": 0, "audit": 0},
                "actual": cleanup.get("leftovers"),
                "pass": cleanup_code == 0 and cleanup.get("leftovers") == {"pending_actions": 0, "agent_runs": 0, "audit": 0},
            })

    print(json.dumps({"marker": marker, "checklist": checklist}, default=str))
    return 0 if all(item["pass"] for item in checklist) else 1


if __name__ == "__main__":
    raise SystemExit(main())
