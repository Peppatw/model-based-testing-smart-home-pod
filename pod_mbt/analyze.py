"""analyze.py — the "AI reads a finished run" step: report.md + proposals/.

Same seam as `Driver` and `Planner`: an interface with a runnable stand-in behind it.
`RuleBasedAnalyst` is deliberately not an LLM — it is rules over the two files a real
analyst would read anyway (`Analysis/ErrorSignature.json`, `Analysis/CoverageReport.json`),
so the seam is exercised and testable without an API key. A real LLM-backed analyst reads
the same run folder and writes the same two things — `Analysis/report.md` and one file per
proposal under `Analysis/proposals/` — and nothing downstream can tell which one wrote them.

What this stand-in does NOT do, on purpose: it never reads `Log/EngineEvents.jsonl`. A
trend with no ErrorSignature to cluster on (a rising `elapsedMs`, a repeated refusal that
never fails its own assertion) only shows up to something reading the raw log — which is
exactly the gap a real LLM analyst closes and this rule-based one is honest about not
attempting.

    python3 -m pod_mbt.run configs/chaos.json --defect accepts_illegal --save runs/
    python3 -m pod_mbt.analyze runs/<the folder just printed>/
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class Analysis:
    """What the AI writes back — the same shape whether a person or a real LLM produced it."""

    report_md: str
    proposals: list[dict[str, Any]] = field(default_factory=list)


class Analyst(Protocol):
    """Swap the implementation, not the shape — same pattern as `Driver` and `Planner`."""

    def analyze(self, run_dir: Path) -> Analysis: ...


def _load(run_dir: Path, rel: str, default: Any) -> Any:
    f = run_dir / rel
    return json.loads(f.read_text()) if f.is_file() else default


class RuleBasedAnalyst:
    """No model behind it, on purpose — the same reason `RuleBasedPlanner` exists in `plan.py`.

    Every signature gets exactly one reading, decided by its `trigger_type`:
    `illegal_call_accepted` is the ambiguous one (guard too strict, or the device is
    missing a check) and becomes a `model_correction` proposal for a person to judge;
    everything else clustered at all, which already means it is worth keeping as a
    regression, and becomes a `regression` proposal carrying the reproduction itself.
    """

    def analyze(self, run_dir: Path) -> Analysis:
        session = _load(run_dir, "session.json", {})
        coverage = _load(run_dir, "Analysis/CoverageReport.json", {})
        signatures = _load(run_dir, "Analysis/ErrorSignature.json", [])
        base_version = session.get("modelVersion")

        lines = [
            f"# {session.get('sessionId', run_dir.name)}",
            "",
            f"Ran `{session.get('config', '?')}` for "
            f"{session.get('durationMs', 0) / 3_600_000:.1f}h, seed {session.get('seed', '?')}, "
            f"modelVersion `{base_version}`.",
            "",
            f"Coverage: transitions {coverage.get('transitions_walked', 0)}/"
            f"{coverage.get('transitions_total', 0)}, "
            f"pairs {coverage.get('pairs_tried', 0)}/{coverage.get('pairs_total', 0)}.",
            "",
        ]

        proposals: list[dict[str, Any]] = []
        if not signatures:
            lines.append("No ErrorSignatures — nothing to propose from this run.")
        else:
            lines.append(f"## {len(signatures)} signature(s)")
            for sig in signatures:
                lines.append(
                    f"- `{sig['trigger_type']}` on `{sig['bug_bucket']}` "
                    f"×{sig['occurrence_count']} (`{sig['signature_hash']}`)")
                if sig["trigger_type"] == "illegal_call_accepted":
                    lines.append(
                        "  Two readings: the guard is stricter than the device needs, "
                        "or the device is missing a check. A person decides which.")
                    proposals.append({
                        "id": f"PROP-{sig['signature_hash']}",
                        "type": "model_correction",
                        "baseVersion": base_version,
                        "target": {"file": "pod_mbt/model.py",
                                   "key": f"{sig['bug_bucket']}.guard"},
                        "current": "guard unchanged",
                        "proposed": "relax or tighten — needs a person's read, not this tool's",
                        "evidence": {"signatureHash": sig["signature_hash"],
                                     "occurrenceCount": sig["occurrence_count"]},
                        "rationale": f"`{sig['bug_bucket']}` was accepted "
                                     f"{sig['occurrence_count']}x when its guard said it "
                                     "shouldn't be.",
                        "status": "pending",
                    })
                else:
                    proposals.append({
                        "id": f"PROP-{sig['signature_hash']}",
                        "type": "regression",
                        "baseVersion": base_version,
                        "target": {"file": "configs/regressions.json", "key": "replay"},
                        "current": [],
                        "proposed": sig["reproduction"],
                        "evidence": {"signatureHash": sig["signature_hash"]},
                        "rationale": f"`{sig['trigger_type']}` on `{sig['bug_bucket']}`, "
                                     f"seen {sig['occurrence_count']}x — worth keeping as a "
                                     "permanent regression.",
                        "status": "pending",
                    })

        return Analysis(report_md="\n".join(lines), proposals=proposals)


def write_back(run_dir: Path, analysis: Analysis) -> None:
    """The only two things an analyst may touch: report.md, and one file per proposal."""
    (run_dir / "Analysis" / "report.md").write_text(analysis.report_md + "\n")
    prop_dir = run_dir / "Analysis" / "proposals"
    prop_dir.mkdir(parents=True, exist_ok=True)
    for p in analysis.proposals:
        (prop_dir / f"{p['id']}.json").write_text(json.dumps(p, indent=2) + "\n")
    index = [{"id": p["id"], "type": p["type"], "status": p["status"]}
             for p in analysis.proposals]
    (prop_dir / "index.json").write_text(json.dumps(index, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Read a finished run folder, write report.md + proposals/.")
    ap.add_argument("run_dir", type=Path)
    a = ap.parse_args(argv)

    analysis = RuleBasedAnalyst().analyze(a.run_dir)
    write_back(a.run_dir, analysis)
    print(f"\n  report         {a.run_dir / 'Analysis' / 'report.md'}")
    print(f"  proposals      {len(analysis.proposals)} written to "
          f"{a.run_dir / 'Analysis' / 'proposals'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
