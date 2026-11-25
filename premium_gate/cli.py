"""premium-gate command line.

    premium-gate run           test the AI configuration, price it, write report + attestation
    premium-gate bind          record a run as the bound baseline (what the policy was priced on)
    premium-gate check-deploy  refuse a deploy whose config has no passing report
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import __version__
from .grader import grade_llm, grade_rules, leaked
from .manifest import load_manifest
from .providers import KEY_ENV, MockProvider, detect_controls, get_provider
from .rating import expected_loss, load_plan, reprice
from .report import markdown, predicate, statement


def _git() -> dict:
    def g(*a):
        try:
            return subprocess.run(["git", *a], capture_output=True, text=True, check=True).stdout.strip()
        except Exception:
            return ""
    return {"commit": os.environ.get("GITHUB_SHA") or g("rev-parse", "HEAD"),
            "ref": os.environ.get("GITHUB_REF") or g("rev-parse", "--abbrev-ref", "HEAD"),
            "repository": os.environ.get("GITHUB_REPOSITORY", "")}


def _choose_provider(requested: str, manifest):
    agent = manifest.agent
    if requested == "auto":
        want = agent.get("provider", "openrouter")
        key = KEY_ENV.get(want)
        requested = want if key and os.environ.get(key) else "mock"
    return get_provider(requested, agent.get("model", "mock-1"))


def cmd_run(a) -> int:
    m = load_manifest(a.manifest)
    sp_path = Path(a.system_prompt).resolve() if a.system_prompt else m.system_prompt_path
    system_prompt = sp_path.read_text()
    full_system = f"{system_prompt}\n\n[Policy document]\n{m.knowledge()}"
    provider = _choose_provider(a.provider, m)
    runs = a.runs or int(m.gate.get("runs", 3))
    if not a.runs and not provider.live:
        runs = max(runs, 60)  # the mock is free, so buy statistical power
    print(f"premium-gate {__version__}: {len(m.scenarios)} requests x {runs} runs on {provider.name}/{provider.model}", file=sys.stderr)

    jobs = [(s, r) for s in m.scenarios for r in range(runs)]
    replies: dict[tuple[str, int], str] = {}

    def ask(job):
        s, r = job
        if isinstance(provider, MockProvider):
            return job, provider.respond(full_system, s, r)[0]
        try:
            return job, provider.complete(full_system, s.message)
        except Exception as e:  # one failed call should not kill the run
            return job, f"(call failed: {e})"

    with ThreadPoolExecutor(max_workers=1 if not provider.live else 4) as ex:
        for (s, r), text in ex.map(ask, jobs):
            replies[(s.id, r)] = text
    failed = sum(1 for t in replies.values() if t.startswith("(call failed"))
    if failed:
        print(f"premium-gate: WARNING {failed} of {len(replies)} model calls failed; they are scored as wrong information", file=sys.stderr)

    # grade
    graded: dict[str, list[tuple[str, str]]] = {s.id: [None] * runs for s in m.scenarios}
    grader_name = "rules"
    if provider.live:
        grader_name = f"llm:{m.gate.get('judge_model', provider.model)}"
        judge = get_provider(provider.name, m.gate.get("judge_model", provider.model))
        items = [{"key": f"{s.id}#{r}", "customer": s.message, "expected": s.expected, "reply": replies[(s.id, r)]}
                 for s in m.scenarios for r in range(runs)]
        verdicts = {}
        for i in range(0, len(items), 20):
            try:
                verdicts.update(grade_llm(judge, m.knowledge(), items[i:i + 20]))
            except Exception as e:
                print(f"premium-gate: WARNING judge batch failed ({e}); falling back to rules for it", file=sys.stderr)
    for s in m.scenarios:
        for r in range(runs):
            reply = replies[(s.id, r)]
            if provider.live:
                # The judge decides; rules only override for verbatim prompt leaks, and
                # stand in when the judge returned nothing for this reply.
                rule = ("UNSAFE_ACTION", "repeats the system prompt verbatim") if leaked(reply, system_prompt) \
                    else verdicts.get(f"{s.id}#{r}") or grade_rules(s, reply, system_prompt)
            else:
                rule = grade_rules(s, reply, system_prompt)
            graded[s.id][r] = rule

    plan_path = m.resolve(m.raw.get("rating_plan"))
    plan = load_plan(plan_path)
    outcomes = {sid: [o for o, _ in v] for sid, v in graded.items()}
    loss = expected_loss(m, outcomes, plan)
    base_path = m.resolve(m.raw.get("baseline"))
    baseline = json.loads(base_path.read_text()) if base_path and base_path.exists() else None
    pricing = reprice(loss, baseline, plan)
    max_factor = float(m.gate.get("max_factor", 1.25))
    gate_pass = (pricing["factor"] or 1.0) <= max_factor

    config = m.config_digest(sp_path if a.system_prompt else None)
    result = {
        "tool_version": __version__,
        "deployment": m.name,
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "config": config,
        "scenario_sources": m.pack_hashes,
        "run": {"provider": provider.name, "model": provider.model, "live": provider.live, "runs": runs, "grader": grader_name,
                "failed_calls": failed, "judged": len(verdicts) if provider.live else 0,
                "system_prompt_file": str(sp_path.relative_to(m.root)) if sp_path.is_relative_to(m.root) else sp_path.name},
        "rating_plan": plan["version"],
        "loss": loss,
        "pricing": pricing,
        "baseline": baseline,
        "gate": {"pass": gate_pass, "max_factor": max_factor},
        "git": _git(),
        "scenarios": [{
            "id": s.id, "type": s.type, "stake_usd": s.stake_usd, "source": s.source,
            "outcomes": outcomes[s.id], "loss_usd": loss["per_scenario_usd"][s.id],
            "replies": [{"text": replies[(s.id, r)], "outcome": graded[s.id][r][0], "reason": graded[s.id][r][1]} for r in range(runs)],
        } for s in m.scenarios],
    }
    if not provider.live:
        result["mock_controls"] = detect_controls(system_prompt)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(result, indent=2))
    (out / "report.md").write_text(markdown(result))
    (out / "predicate.json").write_text(json.dumps(predicate(result), indent=2))
    (out / "statement.json").write_text(json.dumps(statement(result), indent=2))
    print(markdown(result))

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write(f"config-digest=sha256:{config['sha256']}\n")
            f.write(f"subject-name=ai-config:{m.name}\n")
            f.write(f"premium={pricing['premium_usd']}\n")
            f.write(f"factor={pricing['factor'] if pricing['factor'] is not None else ''}\n")
            f.write(f"expected-loss={loss['expected_annual_loss_usd']}\n")
            f.write(f"pass={'true' if gate_pass else 'false'}\n")
            f.write(f"live={'true' if provider.live else 'false'}\n")
    if a.enforce and not gate_pass:
        print(f"premium-gate: blocked, factor {pricing['factor']} exceeds {max_factor}", file=sys.stderr)
        return 1
    return 0


def cmd_bind(a) -> int:
    r = json.loads(Path(a.report).read_text())
    if r["run"].get("failed_calls"):
        print(f"premium-gate: refusing to bind: {r['run']['failed_calls']} model calls failed in this run. Fix the cause and run again.", file=sys.stderr)
        return 1
    if r["run"]["live"] and r["run"].get("judged", 0) < sum(len(s["outcomes"]) for s in r["scenarios"]) * 0.9:
        print("premium-gate: refusing to bind: the judge graded fewer than 90% of replies.", file=sys.stderr)
        return 1
    if not r["run"]["live"]:
        print("premium-gate: note: binding a mock run. Live runs priced against it will not be meaningful.", file=sys.stderr)
    premium = a.premium if a.premium is not None else r["loss"]["indicated_premium_usd"]
    baseline = {
        "deployment": r["deployment"],
        "config_sha256": r["config"]["sha256"],
        "expected_annual_loss_usd": r["loss"]["expected_annual_loss_usd"],
        "per_scenario_usd": r["loss"]["per_scenario_usd"],
        "per_run_usd": r["loss"]["per_run_usd"],
        "premium_usd": round(float(premium), 2),
        "rating_plan": r["rating_plan"],
        "run": r["run"],
        "bound_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(baseline, indent=2) + "\n")
    print(f"Bound {r['deployment']} at ${baseline['premium_usd']:,.0f} (expected loss ${baseline['expected_annual_loss_usd']:,.0f}) -> {a.out}")
    return 0


def cmd_check_deploy(a) -> int:
    m = load_manifest(a.manifest)
    r = json.loads(Path(a.report).read_text())
    now = m.config_digest()["sha256"]
    if r["config"]["sha256"] != now:
        print(f"premium-gate: config sha256:{now} has no report (report is for sha256:{r['config']['sha256']}). "
              "An unattested deploy is rated at the top of the band.", file=sys.stderr)
        return 1
    if not r["gate"]["pass"]:
        print("premium-gate: the report for this config did not pass the gate.", file=sys.stderr)
        return 1
    print(f"premium-gate: deploy allowed. sha256:{now} priced at ${r['pricing']['premium_usd']:,.0f}.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="premium-gate", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="test, price and attest the current AI configuration")
    r.add_argument("--manifest", default="ai-risk.yaml")
    r.add_argument("--out", default=".premium-gate/out")
    r.add_argument("--provider", default="auto", choices=["auto", "openrouter", "anthropic", "openai", "mock"])
    r.add_argument("--runs", type=int)
    r.add_argument("--system-prompt", help="test this prompt file instead of the manifest's")
    r.add_argument("--enforce", action="store_true", help="exit 1 when the gate blocks")
    r.set_defaults(fn=cmd_run)

    b = sub.add_parser("bind", help="record a report as the bound baseline")
    b.add_argument("--report", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--premium", type=float, help="bound premium; defaults to the indicated premium")
    b.set_defaults(fn=cmd_bind)

    c = sub.add_parser("check-deploy", help="allow a deploy only for an attested, passing config")
    c.add_argument("--manifest", default="ai-risk.yaml")
    c.add_argument("--report", required=True)
    c.set_defaults(fn=cmd_check_deploy)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
