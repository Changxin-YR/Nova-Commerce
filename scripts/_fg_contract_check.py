"""Contract validator (temporary helper, kept for re-running with the emitters)."""
import datetime
import json
import pathlib
import sys

REQUIRED = {"gate_id","name","command","cwd","timestamp","duration_ms","exit_code","git","assertions","verdict"}
GIT_REQUIRED = {"revision","subject","relevant_paths","relevant_paths_dirty"}

TARGETS = {
 "FG-01": ["artifacts/evidence/final/fg01_repo_state.json"],
 "FG-02": ["artifacts/evidence/build/fg02_backend_static.txt","artifacts/evidence/build/fg02_backend_static.json"],
 "FG-19": ["artifacts/evidence/storage/fg19_storage.json"],
 "FG-23": ["artifacts/evidence/docker/fg23_health.json"],
 "FG-24": ["artifacts/evidence/security/fg24_secret_scan.json"],
 "FG-25": ["artifacts/evidence/architecture/fg25_architecture.json"],
}

ok = True
for gate, paths in TARGETS.items():
    for p in paths:
        path = pathlib.Path(p)
        if not path.is_file():
            print(f"{gate} {p}: MISSING"); ok = False; continue
        if path.suffix != ".json":
            print(f"{gate} {p}: exists ({path.stat().st_size} bytes), sibling json={path.with_suffix('.json').is_file()}")
            continue
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        problems = []
        missing = REQUIRED - set(data)
        if missing: problems.append(f"missing keys {sorted(missing)}")
        g = data.get("git") or {}
        gmissing = GIT_REQUIRED - set(g)
        if gmissing: problems.append(f"git missing {sorted(gmissing)}")
        if not isinstance(data.get("assertions"), list) or not data["assertions"]:
            problems.append("assertions empty/not a list")
        if not isinstance(data.get("duration_ms"), int): problems.append("duration_ms not int")
        if not isinstance(data.get("exit_code"), int): problems.append("exit_code not int")
        if not g.get("relevant_paths"): problems.append("relevant_paths empty")
        if any(str(x).startswith("artifacts/") for x in g.get("relevant_paths") or []):
            problems.append("relevant_paths contains artifacts/")
        if data.get("verdict") not in {"PASS", "FAIL"}: problems.append("bad verdict")
        for a in data.get("assertions", []):
            if not {"name", "expected", "actual", "pass"} <= set(a):
                problems.append(f"assertion shape {sorted(a)}"); break
            if not isinstance(a["pass"], bool):
                problems.append("assertion pass is not a bool"); break
        try:
            datetime.datetime.fromisoformat(str(data["timestamp"]).replace("Z", "+00:00"))
        except (ValueError, TypeError, AttributeError):
            problems.append(f"timestamp is not ISO-8601: {data.get('timestamp')!r}")
        if data["verdict"] == "PASS":
            if data["exit_code"] != 0: problems.append("PASS with non-zero exit_code")
            if any(a["pass"] is not True for a in data["assertions"]):
                problems.append("PASS with a failing assertion")
        if data["verdict"] == "FAIL" and not data.get("fail_reasons"):
            problems.append("FAIL without fail_reasons")
        print(f"{gate} {p}: {'OK' if not problems else 'PROBLEM'} verdict={data['verdict']} "
              f"exit={data['exit_code']} assertions={len(data['assertions'])} "
              f"duration_ms={data['duration_ms']} relevant_paths_dirty={g.get('relevant_paths_dirty')}")
        for problem in problems:
            print(f"    ! {problem}"); ok = False
print("CONTRACT:", "ALL OK" if ok else "PROBLEMS FOUND")
sys.exit(0 if ok else 1)