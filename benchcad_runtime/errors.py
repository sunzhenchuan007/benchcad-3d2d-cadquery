"""Explicit boundary between repairable submissions and controller failures."""


class SubmissionRejected(ValueError):
    """A known artifact validation failure that may be returned to the agent."""
