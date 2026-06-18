"""Load and validate the YAML config.

Config is the contract: topics, sources, recipients, and settings all live here
so adding a topic or source never touches code. We validate aggressively and
fail with a clear message rather than letting a typo surface deep in the run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

import yaml


class ConfigError(ValueError):
    """Raised when the config is missing required fields or malformed."""


@dataclass
class Topic:
    name: str
    description: str           # plain-English relevance rubric the LLM judges against
    keywords: list[str] = field(default_factory=list)
    extra_rss: list[str] = field(default_factory=list)


@dataclass
class Settings:
    lookback_hours: int = 24
    max_items_per_topic: int = 12
    min_relevance: float = 0.5     # 0..1 cutoff applied to the LLM score
    model: str = "claude-haiku-4-5"
    seen_store_path: str = "state/seen.json"
    seen_ttl_days: int = 30
    sources: list[str] = field(default_factory=lambda: ["federal_register", "google_news"])


@dataclass
class Delivery:
    sender: str = ""
    recipients: list[str] = field(default_factory=list)
    subject_prefix: str = "[Policy Signal]"


@dataclass
class Config:
    topics: list[Topic]
    settings: Settings
    delivery: Delivery
    raw: dict[str, Any] = field(default_factory=dict)


def _as_list(value: Any, label: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"'{label}' must be a list")
    return value


def parse_config(data: dict[str, Any]) -> Config:
    """Validate a parsed YAML dict into a Config. Raises ConfigError on problems."""
    if not isinstance(data, dict):
        raise ConfigError("config root must be a mapping")

    raw_topics = _as_list(data.get("topics"), "topics")
    if not raw_topics:
        raise ConfigError("at least one topic is required")

    topics: list[Topic] = []
    for i, t in enumerate(raw_topics):
        if not isinstance(t, dict):
            raise ConfigError(f"topics[{i}] must be a mapping")
        name = (t.get("name") or "").strip()
        desc = (t.get("description") or "").strip()
        if not name:
            raise ConfigError(f"topics[{i}] missing 'name'")
        if not desc:
            raise ConfigError(f"topic '{name}' missing 'description' (the relevance rubric)")
        topics.append(
            Topic(
                name=name,
                description=desc,
                keywords=[str(k) for k in _as_list(t.get("keywords"), f"{name}.keywords")],
                extra_rss=[str(u) for u in _as_list(t.get("extra_rss"), f"{name}.extra_rss")],
            )
        )

    s = data.get("settings") or {}
    if not isinstance(s, dict):
        raise ConfigError("'settings' must be a mapping")
    settings = Settings(
        lookback_hours=int(s.get("lookback_hours", 24)),
        max_items_per_topic=int(s.get("max_items_per_topic", 12)),
        min_relevance=float(s.get("min_relevance", 0.5)),
        model=str(s.get("model", "claude-haiku-4-5")),
        seen_store_path=str(s.get("seen_store_path", "state/seen.json")),
        seen_ttl_days=int(s.get("seen_ttl_days", 30)),
        sources=[str(x) for x in _as_list(s.get("sources"), "settings.sources")]
        or ["federal_register", "google_news"],
    )
    if not 0.0 <= settings.min_relevance <= 1.0:
        raise ConfigError("settings.min_relevance must be between 0 and 1")

    d = data.get("delivery") or {}
    if not isinstance(d, dict):
        raise ConfigError("'delivery' must be a mapping")
    delivery = Delivery(
        sender=str(d.get("sender", "")),
        recipients=[str(r) for r in _as_list(d.get("recipients"), "delivery.recipients")],
        subject_prefix=str(d.get("subject_prefix", "[Policy Signal]")),
    )

    return Config(topics=topics, settings=settings, delivery=delivery, raw=data)


def apply_delivery_overrides(config: Config, env: dict | None = None) -> Config:
    """Let env vars override delivery details so recipient emails and the sender
    can live in secrets rather than in a (public) committed config file.

      DIGEST_SENDER       -> delivery.sender
      DIGEST_RECIPIENTS   -> delivery.recipients (comma- or whitespace-separated)
    """
    env = env if env is not None else os.environ
    sender = env.get("DIGEST_SENDER")
    if sender:
        config.delivery.sender = sender.strip()
    recipients = env.get("DIGEST_RECIPIENTS")
    if recipients:
        parts = [r.strip() for r in recipients.replace("\n", ",").replace(" ", ",").split(",")]
        config.delivery.recipients = [r for r in parts if r]
    return config


def load_config(path: str, env: dict | None = None) -> Config:
    """Read a YAML file from disk, validate it, and apply env delivery overrides."""
    if not os.path.exists(path):
        raise ConfigError(f"config file not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    config = parse_config(data or {})
    return apply_delivery_overrides(config, env)
