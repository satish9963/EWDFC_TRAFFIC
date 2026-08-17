"""Agent 4 -- decide whether a flow is divertible onto the corridor.

Three rules are supported, because "divertible" means different things to
different projects:

  min_stations       the flow touches at least N corridor stations. The DFC
                     screening basis: enough interaction to be worth diverting.
  min_corridor_km    the flow uses at least N km of corridor. Right when the
                     corridor is long and a flow clipping one end is not a
                     genuine user of it.
  min_corridor_share at least N% of the flow's total journey runs on the
                     corridor. Right when comparing flows of very different
                     lengths, where 50 km means everything to a 60 km haul and
                     nothing to a 2,000 km one.

A rule that cannot be evaluated -- min_corridor_km with no chainage anywhere --
returns UNKNOWN rather than NO, so a missing input never silently reads as a
negative finding.
"""
from core.projects import RULE_MIN_CORRIDOR_KM, RULE_MIN_CORRIDOR_SHARE, RULE_MIN_STATIONS

YES = "YES"
NO = "NO"
UNKNOWN = "UNKNOWN"


class DiversionEngine:
    def __init__(self, eligibility):
        """`eligibility` is a core.projects.EligibilitySettings."""
        self.eligibility = eligibility

    @property
    def threshold(self):
        return self.eligibility.threshold

    def criterion_text(self):
        rule = self.eligibility.rule
        if rule == RULE_MIN_STATIONS:
            return f"at least {self.eligibility.threshold} corridor stations touched"
        if rule == RULE_MIN_CORRIDOR_KM:
            return f"at least {self.eligibility.min_corridor_km:g} km of corridor used"
        return f"at least {self.eligibility.min_corridor_share:.0%} of the route on corridor"

    def decide(self, mapping):
        rule = self.eligibility.rule

        if rule == RULE_MIN_STATIONS:
            verdict = YES if mapping["interaction_count"] >= self.eligibility.threshold else NO
            applied = self.eligibility.threshold

        elif rule == RULE_MIN_CORRIDOR_KM:
            value = mapping.get("corridor_km")
            applied = self.eligibility.min_corridor_km
            if value is None:
                verdict = UNKNOWN
            else:
                verdict = YES if value >= applied else NO

        elif rule == RULE_MIN_CORRIDOR_SHARE:
            value = mapping.get("corridor_share")
            applied = self.eligibility.min_corridor_share
            if value is None:
                verdict = UNKNOWN
            else:
                verdict = YES if value >= applied else NO

        else:  # unreachable: the rule is validated when settings are built
            raise ValueError(f"Unknown eligibility rule {rule!r}")

        return {"Eligible": verdict, "Threshold Applied": applied, "Rule": rule}
