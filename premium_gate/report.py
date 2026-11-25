"""Markdown report (PR comment) and in-toto attestation statement."""
from __future__ import annotations

from collections import Counter

PREDICATE_TYPE = "https://github.com/stefanosimonetto/premium-gate/attestation/v0.1"
LABEL = {"CORRECT": "C", "ESCALATED": "E", "WRONG_INFO": "I", "UNSAFE_ACTION": "U"}


def usd(x: float) -> str:
    return f"${x:,.0f}"


def markdown(result: dict) -> str:
    loss, price, run = result["loss"], result["pricing"], result["run"]
    base = result.get("baseline")
    lines = [f"## premium-gate · `{result['deployment']}`", ""]
    if not run["live"]:
        lines += ["> **Offline mode.** Replies came from the built-in mock model, whose failure rates are hand-set "
                  "assumptions keyed to controls it detects in the system prompt. This run checks the pipeline; it is "
                  "not a risk measurement. Add an `OPENROUTER_API_KEY` secret to price the real model.", ""]
    if run.get("failed_calls"):
        lines += [f"> ⚠️ **{run['failed_calls']} model calls failed** and were scored as wrong information, which overstates the loss. "
                  "Check the API key, model slug and rate limits, then re-run.", ""]
    verdict = "✅ pass" if result["gate"]["pass"] else "⛔ blocked"
    if price["bound"]:
        if price["significant"]:
            move = f"{price['factor']:.2f}×" + (f" (clipped from {price['raw_factor']:.2f}×)" if price.get("clipped") else "")
        else:
            move = "1.00× (change is within run-to-run noise)"
        lines += [
            "| | Bound configuration | This change |",
            "| --- | ---: | ---: |",
            f"| Expected annual loss | {usd(base['expected_annual_loss_usd'])} | {usd(loss['expected_annual_loss_usd'])} "
            f"± {usd(2 * loss['se_usd'])} |",
            f"| Premium | {usd(price['baseline_premium_usd'])} | **{usd(price['premium_usd'])}** |",
            f"| Deployment factor | 1.00× | **{move}** |",
            "",
            f"**Gate:** {verdict} (max factor {result['gate']['max_factor']:.2f}×)",
        ]
    else:
        lines += [f"**Expected annual loss:** {usd(loss['expected_annual_loss_usd'])} ± {usd(2 * loss['se_usd'])} (2 standard errors)  ",
                  f"**Indicated premium:** {usd(price['premium_usd'])}  ",
                  "No bound baseline yet: run `premium-gate bind` to set one."]
    lines += ["", f"### Outcomes ({run['runs']} runs per request)", "",
              "| Request | Type | At stake | Outcomes | Loss / yr | Δ vs bound |", "| --- | --- | ---: | --- | ---: | ---: |"]
    for s in result["scenarios"]:
        cnt = Counter(s["outcomes"])
        c = " ".join(f"{cnt[k]}{LABEL[k]}" for k in LABEL if cnt[k])
        delta = ""
        if base and s["id"] in base.get("per_scenario_usd", {}):
            d = s["loss_usd"] - base["per_scenario_usd"][s["id"]]
            delta = ("+" if d >= 0 else "−") + usd(abs(d))
        lines.append(f"| `{s['id']}` | {s['type']} | {usd(s['stake_usd'])} | {c} | {usd(s['loss_usd'])} | {delta} |")
    counts = Counter(o for s in result["scenarios"] for o in s["outcomes"])
    lines += ["", "C correct · E escalated · I wrong information · U unsafe action (promise, credit or disclosure)  ",
              f"Totals: {counts['CORRECT']} C · {counts['ESCALATED']} E · {counts['WRONG_INFO']} I · {counts['UNSAFE_ACTION']} U"]
    if not run["live"] and result.get("mock_controls"):
        found = [k for k, v in result["mock_controls"].items() if v]
        lines += ["", f"Controls the mock detected in the prompt: {', '.join(found) if found else 'none'}"]
    lines += ["", "<details><summary>Configuration attested</summary>", "",
              f"- Config digest: `sha256:{result['config']['sha256']}`",
              f"- Provider / model: `{run['provider']}` / `{run['model']}` · grader: `{run['grader']}`",
              f"- Rating plan: `{result['rating_plan']}` · tool `premium-gate {result['tool_version']}`",
              "", "</details>"]
    return "\n".join(lines) + "\n"


def predicate(result: dict) -> dict:
    return {
        "tool": {"name": "premium-gate", "version": result["tool_version"]},
        "deployment": result["deployment"],
        "config": result["config"],
        "scenario_sources": result["scenario_sources"],
        "run": result["run"],
        "rating_plan": result["rating_plan"],
        "loss": {k: v for k, v in result["loss"].items() if k != "per_scenario_usd"},
        "pricing": result["pricing"],
        "gate": result["gate"],
        "git": result.get("git", {}),
        "timestamp": result["timestamp"],
    }


def statement(result: dict) -> dict:
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": f"ai-config:{result['deployment']}", "digest": {"sha256": result["config"]["sha256"]}}],
        "predicateType": PREDICATE_TYPE,
        "predicate": predicate(result),
    }
