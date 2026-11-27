"""Behavioural tests for the reviewed SARIF converters in .github/scripts/.

The security-scan upload steps consume whatever these converters emit. The old
inline bandit converter had no missing-file guard, so a scanner that produced
nothing turned its upload step into a red FileNotFoundError; the reviewed
scripts must degrade to valid empty SARIF instead, so the upload always has a
well-formed file.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_bandit_converter_survives_a_missing_report(tmp_path):
    mod = _load("bandit_to_sarif")
    out = tmp_path / "bandit.sarif"
    rc = mod.main(["bandit_to_sarif", str(tmp_path / "bandit.json"), str(out)])
    assert rc == 0
    sarif = json.loads(out.read_text())
    assert sarif["version"] == "2.1.0"
    assert sarif["runs"][0]["results"] == []
    assert sarif["runs"][0]["tool"]["driver"]["name"] == "Bandit"


def test_bandit_converter_maps_a_producing_scan(tmp_path):
    mod = _load("bandit_to_sarif")
    report = tmp_path / "bandit.json"
    report.write_text(json.dumps({
        "results": [{
            "test_id": "B110", "test_name": "try_except_pass",
            "issue_text": "try-except-pass", "issue_severity": "MEDIUM",
            "issue_confidence": "HIGH", "line_number": 12,
            "filename": "cli_anything/x.py", "issue_cwe": {"link": ""},
        }]
    }))
    out = tmp_path / "bandit.sarif"
    assert mod.main(["bandit_to_sarif", str(report), str(out)]) == 0
    sarif = json.loads(out.read_text())
    result = sarif["runs"][0]["results"][0]
    assert result["ruleId"] == "B110"
    assert result["locations"][0]["physicalLocation"]["region"]["startLine"] == 12


def test_pip_audit_converter_survives_a_missing_report(tmp_path):
    mod = _load("pip_audit_to_sarif")
    out = tmp_path / "pip-audit.sarif"
    rc = mod.main(["pip_audit_to_sarif", str(tmp_path / "pip-audit.json"), str(out)])
    assert rc == 0
    sarif = json.loads(out.read_text())
    assert sarif["version"] == "2.1.0"
    assert sarif["runs"][0]["results"] == []
