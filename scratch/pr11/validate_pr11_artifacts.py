import json
from pathlib import Path

ROOT = Path(__file__).parent
for name in ("pr11-runtime.json", "pr11-result.example.json", "pr11-artifact-schema.json"):
    with (ROOT / name).open(encoding="utf-8") as f:
        json.load(f)
source = Path(__file__).resolve().parents[2] / "data" / "protocols" / "PR-11.json"
with source.open(encoding="utf-8") as f:
    protocol = json.load(f)
assert protocol["protocol_id"] == "PR-11"
assert protocol["duration_minutes"]["default"] == 50.0
result = json.loads((ROOT / "pr11-result.example.json").read_text(encoding="utf-8"))
assert result["timer"]["planned_seconds"] == int(protocol["duration_minutes"]["default"] * 60)
assert result["verification"]["gps"] == "disabled"
print("PR-11 artifacts valid; source duration and protocol ID preserved")
