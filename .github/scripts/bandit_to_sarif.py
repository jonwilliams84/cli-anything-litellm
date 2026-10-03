#!/usr/bin/env python3
"""Convert bandit JSON output to SARIF 2.1.0.

Extracted from an inline YAML heredoc into a real file so it can be tested and
reviewed. bandit 1.9.x has no native SARIF formatter, hence this shim.
"""
from __future__ import annotations

import json
import sys

# bandit severity -> SARIF level. SARIF only has error/warning/note, and the
# GitHub Security tab treats "error" as actionable, so MEDIUM maps up to error.
_LEVEL = {"HIGH": "error", "MEDIUM": "error", "LOW": "warning"}


def convert(data: dict) -> dict:
    rules: list[dict] = []
    seen: set[str] = set()
    results: list[dict] = []

    for r in data.get("results", []):
        rid = r.get("test_id", "unknown")
        if rid not in seen:
            seen.add(rid)
            rules.append(
                {
                    "id": rid,
                    "shortDescription": {"text": r.get("test_name", rid)},
                    "fullDescription": {"text": r.get("issue_text", "")},
                    "helpUri": (r.get("issue_cwe") or {}).get("link", ""),
                    "properties": {
                        "security-severity": {
                            "HIGH": "8.0",
                            "MEDIUM": "5.0",
                            "LOW": "2.0",
                        }.get(r.get("issue_severity", "LOW"), "2.0")
                    },
                }
            )
        results.append(
            {
                "ruleId": rid,
                "level": _LEVEL.get(r.get("issue_severity", "LOW"), "warning"),
                "message": {"text": r.get("issue_text", "")},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": r.get("filename", "unknown")},
                            "region": {"startLine": max(1, int(r.get("line_number", 1)))},
                        }
                    }
                ],
                "properties": {
                    "confidence": r.get("issue_confidence", ""),
                    "severity": r.get("issue_severity", ""),
                },
            }
        )

    return {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": [
            {
                "tool": {"driver": {"name": "Bandit", "informationUri": "https://bandit.readthedocs.io/", "rules": rules}},
                "results": results,
            }
        ],
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} <bandit.json> <out.sarif>", file=sys.stderr)
        return 2
    src, dst = argv[1], argv[2]
    try:
        with open(src) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        # A scanner that produced nothing must still yield valid empty SARIF,
        # otherwise upload-sarif fails and takes the whole advisory run with it.
        data = {"results": []}
    except json.JSONDecodeError as exc:
        print(f"warning: {src} is not valid JSON ({exc}); emitting empty SARIF", file=sys.stderr)
        data = {"results": []}

    sarif = convert(data)
    with open(dst, "w") as fh:
        json.dump(sarif, fh, indent=2)
    print(f"converted {len(sarif['runs'][0]['results'])} bandit findings -> {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
