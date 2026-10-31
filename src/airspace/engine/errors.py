"""Engine error types."""

from __future__ import annotations


class PlanningError(RuntimeError):
    """The engine cannot produce a sound answer for this scenario."""


class CandidateCapExceededError(PlanningError):
    """The candidate catalogue would exceed the configured cap.

    Raised instead of truncating, so no incomplete search is ever reported as optimal.
    """

    def __init__(self, count: int, cap: int):
        self.count = count
        self.cap = cap
        super().__init__(
            f"candidate catalogue has {count} plans, exceeding candidate_cap={cap}; "
            "refusing to truncate and claim an optimum"
        )


class UnknownPlanError(PlanningError):
    pass


class InconsistentAssessmentsError(PlanningError):
    pass
