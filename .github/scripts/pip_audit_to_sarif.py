#!/usr/bin/env python3
"""Convert pip-audit JSON output to SARIF 2.1.0."""
from __future__ import annotations

import json
import sys


def convert(data: dict) -> dict:
    rules: list[dict] = []
    seen: set[str] = set()
    results: list[dict] = []

    for dep in data.get("dependencies", []):
        name = dep.get("name", "?")
        version = dep.get("version", "?")
        for vuln in dep.get("vulns", []):
            vid = vuln.get("id", "unknown")
            fix = ", ".join(vuln.get("fix_versions", [])) or "none published"
            if vid not in seen:
                seen.add(vid)
                rules.append({
                    "id": vid,
                    "shortDescription": {"text": f"{vid} affects {name}"},
                    "fullDescription": {"text": vuln.get("description", "")[:1000]},
                    "properties": {"security-severity": "7.0"},
                })
            results.append({
                "ruleId": vid,
                "level": "error",
                "message": {"text": f"{name} {version}: {vuln.get('description','')[:300]} (fix: {fix})"},
                "locations": [{
                    "physicalLocation": {
                        "artifactLocation": {"uri": "setup.py"},
                        "region": {"startLine": 1},
                    }
                }],
            })

    return {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": [{
            "tool": {"driver": {"name": "pip-audit", "informationUri": "https://pypi.org/project/pip-audit/", "rules": rules}},
            "results": results,
        }],
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} <pip-audit.json> <out.sarif>", file=sys.stderr)
        return 2
    try:
        with open(argv[1]) as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"warning: {argv[1]} unusable ({exc}); emitting empty SARIF", file=sys.stderr)
        data = {"dependencies": []}

    sarif = convert(data)
    with open(argv[2], "w") as fh:
        json.dump(sarif, fh, indent=2)
    print(f"converted {len(sarif['runs'][0]['results'])} pip-audit findings -> {argv[2]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
