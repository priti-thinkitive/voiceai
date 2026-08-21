"""IpRange — the real, fixed CIDR ranges a Platform X customer must whitelist
on their own SIP trunk provider (Twilio, Telnyx, Vonage, etc.) for BYO SIP
(`POST /agents/{agent_id}/numbers/byo`) calls to actually reach our voice
infrastructure.

Confirmed via a live WebFetch of the voice vendor's own current custom-
telephony docs (done 2026-08-21) — these are the vendor's real, complete set
of 5 SIP-signaling/media IP ranges, not guessed or partial. The vendor's own
docs do not state these are permanently fixed, so this data is hand-verified
and dated (`last_verified`), not asserted as never-changing — see
app/routers/telephony.py's module docstring for the full reasoning on why
this is a real API endpoint and not just static customer-doc text.

Prior to this endpoint, these 5 ranges only existed as hand-typed text in
vendor-docs/Getting-Started.html — a real drift risk (the vendor changes the
ranges, our hardcoded doc text silently goes stale with no way to catch it).
This module is now the single source of truth both the API and the customer
doc should stay in sync with.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# The date these 5 ranges were last confirmed live against the voice
# vendor's own current documentation. A literal, hand-set string — never a
# dynamic "as of right now" timestamp, since this data is hand-verified on
# an as-needed basis, not fetched live from the vendor on every request.
LAST_VERIFIED = "2026-08-21"


class IpRangeEntry(BaseModel):
    """One CIDR range that must be whitelisted, with a short plain-language
    label describing what traffic it covers.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "cidr": "18.98.16.120/30",
                "label": "All regions",
            }
        }
    )

    cidr: str = Field(description="A CIDR block to whitelist on your own SIP trunk provider.")
    label: str = Field(
        description="A short, plain-language description of what traffic this range covers."
    )


# The 5 real, current CIDR ranges, each with the vendor's own plain-language
# label for what traffic it covers. Order matches the source doc.
IP_RANGES: list[IpRangeEntry] = [
    IpRangeEntry(cidr="18.98.16.120/30", label="All regions"),
    IpRangeEntry(cidr="3.42.144.0/23", label="All regions"),
    IpRangeEntry(cidr="153.57.128.0/18", label="All regions"),
    IpRangeEntry(cidr="143.223.88.0/21", label="Certain United States traffic"),
    IpRangeEntry(cidr="161.115.160.0/19", label="Certain United States traffic"),
]
