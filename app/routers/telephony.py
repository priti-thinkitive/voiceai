"""GET /telephony/ip-ranges — the fixed set of CIDR ranges a Platform X
customer must whitelist on their own SIP trunk provider (Twilio, Telnyx,
Vonage, etc.) for BYO SIP (`POST /agents/{agent_id}/numbers/byo`) calls to
actually reach our voice infrastructure.

**Why this is a real API endpoint, not just customer-doc text.** These 5
ranges previously existed only as hand-typed text in
vendor-docs/Getting-Started.html. Our voice infrastructure provider's own
docs do not state these ranges are permanently fixed, so hand-typed doc text
carries a real drift risk: if the ranges ever change, the doc silently goes
stale with nothing to catch it. Making this a real, structured endpoint
means app/models/telephony.py's `IP_RANGES` is the single source of truth,
the customer doc can point at it as the authoritative reference, and a
future automated check could diff this endpoint's `last_verified` date
against how recently someone actually re-confirmed the values.

**Deliberately mirrors GET /languages' pattern exactly** (see
app/routers/languages.py for the fuller reasoning this endpoint reuses
without repeating): a small, fully static, hardcoded list with no vendor
call and no realistic growth, so no pagination; requires
`get_current_platform` for consistency with the rest of this authenticated
API surface,
even though the data isn't platform-owned and costs nothing per-call; not
platform_id-scoped, so no cross-platform-isolation test applies.
"""

from __future__ import annotations

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict

from app.deps import CurrentPlatform
from app.models.telephony import IP_RANGES, LAST_VERIFIED, IpRangeEntry

router = APIRouter(prefix="/telephony", tags=["telephony"])


class IpRangeListResponse(BaseModel):
    """`GET /telephony/ip-ranges` response envelope."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "items": [
                    {"cidr": "18.98.16.120/30", "label": "All regions"},
                    {"cidr": "3.42.144.0/23", "label": "All regions"},
                    {"cidr": "153.57.128.0/18", "label": "All regions"},
                    {"cidr": "143.223.88.0/21", "label": "Certain United States traffic"},
                    {"cidr": "161.115.160.0/19", "label": "Certain United States traffic"},
                ],
                "total_count": 5,
                "last_verified": "2026-08-21",
            }
        }
    )

    items: list[IpRangeEntry]
    total_count: int
    last_verified: str


@router.get(
    "/ip-ranges",
    response_model=IpRangeListResponse,
    status_code=status.HTTP_200_OK,
    summary="List the IP ranges to whitelist on your SIP trunk provider for BYO SIP",
)
async def list_ip_ranges(caller: CurrentPlatform) -> IpRangeListResponse:
    """List the fixed set of CIDR ranges that must be whitelisted on your own
    SIP trunk provider (Twilio, Telnyx, Vonage, or another provider) for
    calls to reach us after `POST /agents/{agent_id}/numbers/byo`.

    A small, fixed, hand-verified reference list — no pagination or
    filtering involved. `last_verified` is the date these ranges were last
    confirmed current; treat this endpoint, not any static doc text, as the
    authoritative source.
    """
    del caller  # auth-only: proves a valid API key, not used for scoping

    return IpRangeListResponse(
        items=IP_RANGES,
        total_count=len(IP_RANGES),
        last_verified=LAST_VERIFIED,
    )
