"""Loading and validating project presets.

A ProjectSettings object is the single thing the pipeline needs to know about
which railway project it is assessing. Agents take one of these rather than
reading module-level constants, which is what makes the same code serve a DFC,
a new line and a doubling scheme without edits.
"""
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

PRESETS_FILE = Path(__file__).resolve().parent.parent / "projects.yaml"

RULE_MIN_STATIONS = "min_stations"
RULE_MIN_CORRIDOR_KM = "min_corridor_km"
RULE_MIN_CORRIDOR_SHARE = "min_corridor_share"
RULES = (RULE_MIN_STATIONS, RULE_MIN_CORRIDOR_KM, RULE_MIN_CORRIDOR_SHARE)

RULE_LABELS = {
    RULE_MIN_STATIONS: "Minimum corridor stations touched",
    RULE_MIN_CORRIDOR_KM: "Minimum corridor length used (km)",
    RULE_MIN_CORRIDOR_SHARE: "Minimum share of route on corridor",
}


@dataclass(frozen=True)
class MatchingSettings:
    """How a route station is judged to be on the corridor."""
    by_code: bool = True
    by_proximity: bool = False
    buffer_km: float = 5.0

    def __post_init__(self):
        if not (self.by_code or self.by_proximity):
            raise ValueError(
                "A project must match by code, by proximity, or both -- "
                "with neither enabled no route can ever touch the corridor."
            )
        if self.buffer_km <= 0:
            raise ValueError(f"buffer_km must be positive, got {self.buffer_km}")


@dataclass(frozen=True)
class EligibilitySettings:
    """What counts as divertible traffic."""
    rule: str = RULE_MIN_STATIONS
    threshold: int = 2
    thresholds_offered: tuple = (2, 3)
    min_corridor_km: float = None
    min_corridor_share: float = None

    def __post_init__(self):
        if self.rule not in RULES:
            raise ValueError(f"Unknown eligibility rule {self.rule!r}; expected one of {RULES}")
        if self.threshold < 1:
            raise ValueError(f"threshold must be at least 1, got {self.threshold}")
        if self.rule == RULE_MIN_CORRIDOR_KM and not self.min_corridor_km:
            raise ValueError("rule min_corridor_km requires min_corridor_km to be set")
        if self.rule == RULE_MIN_CORRIDOR_SHARE and not self.min_corridor_share:
            raise ValueError("rule min_corridor_share requires min_corridor_share to be set")
        if self.min_corridor_share is not None and not 0 < self.min_corridor_share <= 1:
            raise ValueError(
                f"min_corridor_share is a fraction between 0 and 1, got {self.min_corridor_share}"
            )

    @property
    def label(self):
        return RULE_LABELS[self.rule]


@dataclass(frozen=True)
class ProjectSettings:
    key: str
    name: str
    short_name: str
    description: str = ""
    matching: MatchingSettings = field(default_factory=MatchingSettings)
    eligibility: EligibilitySettings = field(default_factory=EligibilitySettings)
    cache_max_age_days: int = 365

    def with_threshold(self, threshold):
        return replace(self, eligibility=replace(self.eligibility, threshold=int(threshold)))

    def with_overrides(self, **kwargs):
        """Apply sidebar overrides, returning a new settings object.

        Accepts flat keys (threshold, rule, buffer_km, by_proximity, ...) so the
        UI does not need to know how the dataclasses nest.
        """
        matching_keys = {"by_code", "by_proximity", "buffer_km"}
        eligibility_keys = {"rule", "threshold", "thresholds_offered",
                            "min_corridor_km", "min_corridor_share"}

        matching_updates = {k: v for k, v in kwargs.items() if k in matching_keys}
        eligibility_updates = {k: v for k, v in kwargs.items() if k in eligibility_keys}
        top_updates = {k: v for k, v in kwargs.items()
                       if k not in matching_keys and k not in eligibility_keys}

        updated = self
        if matching_updates:
            updated = replace(updated, matching=replace(updated.matching, **matching_updates))
        if eligibility_updates:
            updated = replace(updated,
                              eligibility=replace(updated.eligibility, **eligibility_updates))
        if top_updates:
            updated = replace(updated, **top_updates)
        return updated


def _build(key, block):
    matching_block = block.get("matching") or {}
    eligibility_block = dict(block.get("eligibility") or {})
    cache_block = block.get("cache") or {}

    offered = eligibility_block.pop("thresholds_offered", None) or [2, 3]
    threshold = eligibility_block.pop("threshold", offered[0])

    try:
        matching = MatchingSettings(
            by_code=bool(matching_block.get("by_code", True)),
            by_proximity=bool(matching_block.get("by_proximity", False)),
            buffer_km=float(matching_block.get("buffer_km", 5.0)),
        )
        eligibility = EligibilitySettings(
            rule=eligibility_block.get("rule", RULE_MIN_STATIONS),
            threshold=int(threshold),
            thresholds_offered=tuple(int(t) for t in offered),
            min_corridor_km=eligibility_block.get("min_corridor_km"),
            min_corridor_share=eligibility_block.get("min_corridor_share"),
        )
    except (ValueError, TypeError) as exc:
        # Name the offending project; otherwise a typo in one block reads as a
        # generic startup crash with no clue which preset caused it.
        raise ValueError(f"Project preset {key!r} is invalid: {exc}") from exc

    return ProjectSettings(
        key=key,
        name=block.get("name", key),
        short_name=block.get("short_name", key.upper()),
        description=(block.get("description") or "").strip(),
        matching=matching,
        eligibility=eligibility,
        cache_max_age_days=int(cache_block.get("max_age_days", 365)),
    )


def load_projects(path=PRESETS_FILE):
    """Return ({key: ProjectSettings}, default_key)."""
    if not Path(path).exists():
        fallback = ProjectSettings(key="custom", name="Custom corridor", short_name="Custom")
        return {"custom": fallback}, "custom"

    with open(path, "r", encoding="utf-8") as fh:
        document = yaml.safe_load(fh) or {}

    blocks = document.get("projects") or {}
    if not blocks:
        raise ValueError(f"{path} defines no projects")

    projects = {key: _build(key, block or {}) for key, block in blocks.items()}
    default_key = document.get("default")
    if default_key not in projects:
        default_key = next(iter(projects))
    return projects, default_key
