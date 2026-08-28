"""Shared versions and time boundaries for prospective source-lead capture."""

from datetime import UTC, datetime

CAPTURE_VERSION = "source_lead_prospective_capture_v1"
OPERATIONAL_COHORT_START = datetime(2026, 8, 2, tzinfo=UTC)

# The date identity registry v2 (source_lead_qualification.py) is frozen and
# populated with real, evidenced links -- research/gate-source-lead-
# registry-activation-v2. A capture whose source_first_observed_at is before
# this must never be treated as a v2-qualified prospective result even if its
# identity happens to satisfy a v2 registry link: identity was not confirmed
# at the time that capture occurred, only retroactively (colleague review,
# 2026-08-28, on the sibling evidence-capture PR -- "не применять текущие
# каталоги ретроактивно"). Same day-level precision as
# OPERATIONAL_COHORT_START, not deploy-second precision -- this repo's other
# cohort boundaries use the same convention.
#
# Deliberately set a few days past this line's own authoring date (colleague
# review, 2026-08-28, second round): the original value was today at
# midnight UTC, which is *before* this PR could realistically merge and
# deploy -- a capture in the gap between midnight and actual deploy would
# satisfy ">=" while the v2 code was not even running yet. Bump this to the
# actual deploy date if it lands after this value; never move it earlier.
IDENTITY_REGISTRY_V2_START = datetime(2026, 8, 30, tzinfo=UTC)
