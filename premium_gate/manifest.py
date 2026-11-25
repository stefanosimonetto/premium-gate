"""Load and hash the ai-risk.yaml manifest and the files it points to."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import PACKS_DIR


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


@dataclass
class Scenario:
    id: str
    type: str            # benign | ambiguous | pressure | injection | extraction
    stake_usd: float
    message: str
    expected: str
    expect_any: list[str]
    weight: float = 1.0
    source: str = ""
    mock: dict = field(default_factory=dict)


@dataclass
class Manifest:
    path: Path
    root: Path
    raw: dict
    system_prompt_path: Path
    knowledge_paths: list[Path]
    scenarios: list[Scenario]
    pack_hashes: dict[str, str]

    # convenience accessors -------------------------------------------------
    @property
    def name(self) -> str:
        return self.raw.get("deployment", {}).get("name", "ai-deployment")

    @property
    def deployment(self) -> dict:
        return self.raw.get("deployment", {})

    @property
    def agent(self) -> dict:
        return self.raw.get("agent", {})

    @property
    def gate(self) -> dict:
        return self.raw.get("gate", {})

    def resolve(self, rel: str | None) -> Path | None:
        return (self.root / rel).resolve() if rel else None

    def system_prompt(self) -> str:
        return self.system_prompt_path.read_text()

    def knowledge(self) -> str:
        return "\n\n".join(p.read_text() for p in self.knowledge_paths)

    def config_digest(self, system_prompt_override: Path | None = None) -> dict:
        """Hashes of everything that defines the deployed AI behaviour.

        The combined digest is the attestation subject: if any of these files
        change, the deployment is a different risk and needs a new attestation.
        """
        sp = system_prompt_override or self.system_prompt_path
        parts = {
            "manifest": sha256_file(self.path),
            "system_prompt": sha256_file(sp),
            "model": sha256_bytes(f"{self.agent.get('provider')}/{self.agent.get('model')}".encode()),
        }
        for p in self.knowledge_paths:
            parts[f"knowledge:{p.name}"] = sha256_file(p)
        combined = sha256_bytes(json.dumps(parts, sort_keys=True).encode())
        return {"sha256": combined, "parts": parts}


def _load_scenarios(path: Path, source: str) -> list[Scenario]:
    items = yaml.safe_load(path.read_text()) or []
    out = []
    for it in items:
        out.append(Scenario(
            id=str(it["id"]), type=it.get("type", "benign"), stake_usd=float(it.get("stake_usd", 0)),
            message=it["message"], expected=it.get("expected", ""), expect_any=list(it.get("expect_any", [])),
            weight=float(it.get("weight", 1.0)), source=source, mock=it.get("mock", {}) or {},
        ))
    return out


def load_manifest(path: str | Path) -> Manifest:
    path = Path(path).resolve()
    raw = yaml.safe_load(path.read_text())
    root = path.parent
    agent = raw.get("agent", {})
    sp = (root / agent["system_prompt"]).resolve()
    knowledge = [(root / k).resolve() for k in agent.get("knowledge", [])]
    scenarios: list[Scenario] = []
    pack_hashes: dict[str, str] = {}
    for rel in raw.get("scenarios", []):
        p = (root / rel).resolve()
        scenarios += _load_scenarios(p, rel)
        pack_hashes[rel] = sha256_file(p)
    for pack in raw.get("packs", []):
        p = PACKS_DIR / f"{pack}.yaml"
        if not p.exists():
            raise SystemExit(f"Unknown built-in pack: {pack}")
        scenarios += _load_scenarios(p, f"pack:{pack}")
        pack_hashes[f"pack:{pack}"] = sha256_file(p)
    ids = [s.id for s in scenarios]
    if len(ids) != len(set(ids)):
        raise SystemExit("Scenario ids must be unique across files and packs")
    return Manifest(path, root, raw, sp, knowledge, scenarios, pack_hashes)
