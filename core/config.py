"""Loads config.yaml plus environment overrides. No secrets live here."""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    domain: str
    bucket: str
    region: str
    site_dir: Path
    required_pages: list[str]
    alt_names: list[str] = field(default_factory=list)
    distribution_id: str = ""      # auto-discovered from the domain alias if blank
    hosted_zone_id: str = ""       # auto-discovered from the domain name if blank
    oac_name: str = "csce412-site-oac"
    model: str = "gpt-4.1-mini"
    max_agent_steps: int = 12

    @property
    def all_names(self) -> list[str]:
        return [self.domain, *self.alt_names]


def load_config(path: str | os.PathLike | None = None) -> Config:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    path = Path(path or os.environ.get("P5_CONFIG", ROOT / "config.yaml"))
    raw = yaml.safe_load(path.read_text())
    return Config(
        domain=raw["domain"],
        alt_names=raw.get("alt_names", []),
        bucket=raw["bucket"],
        region=os.environ.get("AWS_REGION", raw.get("region", "us-east-1")),
        site_dir=(ROOT / raw.get("site_dir", "site")).resolve(),
        required_pages=raw["required_pages"],
        distribution_id=raw.get("distribution_id", "") or "",
        hosted_zone_id=raw.get("hosted_zone_id", "") or "",
        oac_name=raw.get("oac_name", "csce412-site-oac"),
        model=os.environ.get("OPENAI_MODEL", raw.get("model", "gpt-4.1-mini")),
        max_agent_steps=int(raw.get("max_agent_steps", 12)),
    )
