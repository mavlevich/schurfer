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
#
# Frozen history: this cutover is no longer live (research/gate-source-
# lead-registry-activation-v3, PR 3 of 3, replaced it with
# IDENTITY_REGISTRY_V3_START below), kept only because captures made under
# it are still on disk and its own value must never move. Nothing in this
# module's current code path reads it any more.
IDENTITY_REGISTRY_V2_START = datetime(2026, 8, 30, tzinfo=UTC)

# research/gate-source-lead-registry-activation-v3 (PR 3 of 3): replaces
# IDENTITY_REGISTRY_V2_START as the live cutover, not stacked alongside it --
# qualify_source_lead and capture_claimed_source_leads both switched to this
# constant entirely. Deliberately later than IDENTITY_REGISTRY_V2_START, not
# just a v3-flavored copy of the same date: a capture whose identity was
# confirmed under registry v2 alone is not enough any more now that route
# evidence for the derivative markets themselves is required
# (ROUTE_EVIDENCE_INDEPENDENTLY_VERIFIED, source_lead_qualification.py) --
# only a capture made at or after the v3 registry (with that route evidence)
# was actually live may be treated as v3-qualified prospective evidence.
# Same "bump to the actual deploy date if it lands later, never move it
# earlier" rule as IDENTITY_REGISTRY_V2_START above.
IDENTITY_REGISTRY_V3_START = datetime(2026, 9, 3, tzinfo=UTC)
