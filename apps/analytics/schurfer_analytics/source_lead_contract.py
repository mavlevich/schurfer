"""Shared versions and time boundaries for prospective source-lead capture."""

from datetime import UTC, datetime

CAPTURE_VERSION = "source_lead_prospective_capture_v1"
OPERATIONAL_COHORT_START = datetime(2026, 8, 2, tzinfo=UTC)

# The date identity registry v2 (source_lead_qualification.py) was frozen and
# populated with real, evidenced links -- research/gate-source-lead-
# registry-activation-v2. A capture whose source_first_observed_at is before
# this must never be treated as a v2-qualified prospective result even if its
# identity happens to satisfy a v2 registry link: identity was not confirmed
# at the time that capture occurred, only retroactively (colleague review,
# 2026-08-28, on the sibling evidence-capture PR -- "не применять текущие
# каталоги ретроактивно"). Same day-level precision as
# OPERATIONAL_COHORT_START, not deploy-second precision -- this repo's other
# cohort boundaries use the same convention.
IDENTITY_REGISTRY_V2_START = datetime(2026, 8, 28, tzinfo=UTC)
