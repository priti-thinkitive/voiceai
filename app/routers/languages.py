"""GET /languages — the closed set of language/locale codes Platform X can
pass as `CreateAgentRequest.language` on `POST /agents` (see
app/models/language.py, app/models/agent.py).

**Deliberately NOT built the way GET /voices was, and that's a considered
exception to this project's "pagination is not optional" list-endpoint rule
(see the standards doc's "List endpoints — search, filter, and pagination
are not optional" section), not an oversight.** That rule targets
vendor-originated catalogs that are large and can change/grow independently
of us (GET /voices: ~300 entries, fetched live from the voice vendor on
every request, no static upper bound). This endpoint is the opposite shape
in every relevant way: a small (63-entry), fully static, hardcoded reference
list baked into our own code (app/models/language.py's `Language` enum) —
there is no vendor call, no growth between requests, and no realistic
scenario where paging through 63 items in one response is a problem. Adding
limit/offset here would be pure ceremony with no real benefit to Platform X.
Revisit only if this list ever becomes vendor-fetched/dynamic instead of a
hardcoded enum.

**Auth decision, reasoned through rather than defaulted**: requires
`get_current_platform`, same as every other endpoint in this API, even
though serving it costs us nothing per-call (no vendor call, unlike GET
/voices) and the data itself isn't platform-owned. Chosen for consistency
with the rest of the API surface being authenticated infrastructure — every
other endpoint in this project (including GET /voices, which shares the
"not platform-owned data" property) requires a valid API key, and carving
out an unauthenticated exception here would be a one-off inconsistency with
no real benefit: Platform X already has a valid API key for every other call
they make, so requiring one here adds no real friction, and keeping every
endpoint behind the same auth gate keeps the security model simple to reason
about (one rule, not "authenticated except these specific endpoints").
Revisit only if a real, concrete need for a pre-auth/public consumer shows
up (e.g. a public marketing page listing supported languages).

Not platform_id-scoped — same reasoning as GET /voices: every platform sees
the identical static list, so there's no `get_platform_filter` tenancy
scoping and no cross-platform-isolation test for this endpoint.
"""

from __future__ import annotations

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict

from app.deps import CurrentPlatform
from app.models.language import LANGUAGE_NAMES, Language

router = APIRouter(prefix="/languages", tags=["languages"])


class LanguageEntry(BaseModel):
    """One supported language/locale code."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "code": "yue-CN",
                "name": "Cantonese (Mainland, not Hong Kong)",
            }
        }
    )

    code: Language
    name: str


class LanguageListResponse(BaseModel):
    """`GET /languages` response envelope."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "items": [
                    {"code": "en-US", "name": "English (United States)"},
                    {"code": "zh-CN", "name": "Mandarin Chinese (China)"},
                    {"code": "yue-CN", "name": "Cantonese (Mainland, not Hong Kong)"},
                ],
                "total_count": 63,
            }
        }
    )

    items: list[LanguageEntry]
    total_count: int


@router.get(
    "",
    response_model=LanguageListResponse,
    status_code=status.HTTP_200_OK,
    summary="List every language/locale code accepted by POST /agents' language field",
)
async def list_languages(caller: CurrentPlatform) -> LanguageListResponse:
    """List the full, static set of language/locale codes usable as
    `CreateAgentRequest.language` on `POST /agents`.

    A small, fixed, hardcoded reference list (63 codes) — no pagination,
    filtering, or vendor call involved; see this module's docstring for why
    that's a deliberate, justified exception to this API's usual list-
    endpoint pagination rule, not an oversight. Every platform sees the
    identical list, so there's no tenancy scoping here.

    Note `yue-CN` — it's Cantonese, but specifically the Mainland China
    variant; there is no separate Hong Kong Cantonese code.
    """
    del caller  # auth-only: proves a valid API key, not used for scoping

    items = [LanguageEntry(code=code, name=name) for code, name in LANGUAGE_NAMES.items()]
    return LanguageListResponse(items=items, total_count=len(items))
