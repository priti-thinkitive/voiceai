"""GET /voices — closes the documented gap: Platform X previously had no way
to discover a valid `voice_id` for POST /agents (see the standards doc's
Feature status section, POST /agents entry, "Known gap" paragraph). Exposes
Retell's own real voice catalog directly, per the fix already decided there.

This is a read-through proxy to Retell, not a synced/cached local
collection — every request calls Retell live via retell_adapter.list_voices()
(see that function's docstring for the confirmed real field set). No
`Voices` Mongo collection, no persistence.

Requires auth (`get_current_platform`, same as every other endpoint) but is
NOT platform_id-scoped data — every platform sees Retell's same catalog, so
there is no `get_platform_filter` tenancy scoping here and no cross-platform-
isolation test for this endpoint specifically, unlike POST /agents.

This is the first list endpoint in the project, so it sets the pagination/
filtering pattern future list endpoints (e.g. GET /calls) should follow:
`limit`/`offset` query params (limit capped at 100 server-side regardless of
what's requested), response envelope is `{"items": [...], "total_count": N,
"limit": ..., "offset": ...}` — `total_count` (rather than a bare `has_more`
flag) is the chosen pattern since it lets a caller compute both "are there
more" and "which page am I on" from one field, and Retell's own list is
small enough (~300 items) that computing an exact count after our own
filtering is cheap, unlike a genuinely large collection where a full count
could be expensive.

**GET /voices/{voice_id}/preview — vendor-identity-leak fix.** A real, live
bug was found and confirmed: `preview_audio_url` on `VoicePublic` originally
passed through Retell's raw S3 URL verbatim (e.g.
`https://retell-utils-public.s3.us-west-2.amazonaws.com/cartesia-....mp3`),
putting the literal string "retell" directly in the domain of a URL any
Platform X developer could inspect in the response body or browser dev
tools — the same class of defect as a leaked `retell_call_id` field (see
the standards doc: "a leaked retell_call_id field in a response is a real
defect, not a style nit"), just via a URL string instead of a field name.

The fix applies the exact pattern vendor-docs/Full-System-Architecture.html
already establishes for call recordings/transcripts — re-host under our own
domain rather than ever handing Platform X a Retell URL directly — to voice
preview audio too. This endpoint looks up the voice's real
`preview_audio_url` server-side (via retell_adapter.get_voice(), Retell's
real documented `GET /get-voice/{voice_id}` — see vendor-docs/Retell.md),
fetches the actual audio bytes server-side (retell_adapter.
fetch_preview_audio(), a real httpx GET, never a redirect — a redirect would
just hand the browser Retell's real URL directly, defeating the point), and
streams them back under our own domain/path. `VoicePublic.preview_audio_url`
is now a relative path on our own API (`/voices/{voice_id}/preview`)
instead of Retell's raw URL — see app/models/voice.py's docstring for the
full before/after.

No caching/persistence of the audio bytes — same "don't build for
hypothetical futures" reasoning as GET /voices' own read-through-not-cached
decision; preview audio is small and fetched on demand, revisit only if this
becomes an observed real performance problem.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from app.config import Settings, get_settings
from app.deps import CurrentPlatform
from app.errors import CODE_NOT_FOUND, AppError
from app.models.voice import VoicePublic
from app.services import retell_adapter

router = APIRouter(prefix="/voices", tags=["voices"])

_MAX_LIMIT = 100
_DEFAULT_LIMIT = 20

_EXAMPLE_VOICE: dict[str, Any] = {
    "voice_id": "11labs-Adrian",
    "voice_name": "Adrian",
    "provider": "elevenlabs",
    "gender": "male",
    "accent": "American",
    "age": "Middle Aged",
    "preview_audio_url": "/voices/11labs-Adrian/preview",
    "recommended": True,
}


class VoiceListResponse(BaseModel):
    """`GET /voices` response envelope."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "items": [_EXAMPLE_VOICE],
                "total_count": 1,
                "limit": 20,
                "offset": 0,
            }
        }
    )

    items: list[VoicePublic]
    total_count: int
    limit: int
    offset: int


def _to_public(voice: dict[str, object]) -> VoicePublic | None:
    """Map one raw Retell voice object onto our own VoicePublic shape.

    Returns None (skip) if a required field is missing/wrong-typed — this is
    defensive against a genuinely malformed entry from Retell rather than an
    expected case; skipping one bad entry beats failing the whole list.

    `preview_audio_url` is deliberately NOT copied from Retell's raw field —
    that value is Retell's own S3 URL (vendor-identity leak, see this
    module's docstring for the full bug history). We only need to confirm
    Retell *has* a preview URL for this voice (an entry with none shouldn't
    claim to have a working preview); the value we actually expose is always
    our own `/voices/{voice_id}/preview` path.
    """
    fields = {
        key: voice.get(key)
        for key in ("voice_id", "voice_name", "provider", "gender", "accent", "age")
    }
    has_preview = isinstance(voice.get("preview_audio_url"), str) and bool(
        voice.get("preview_audio_url")
    )
    if not all(isinstance(v, str) and v for v in fields.values()) or not has_preview:
        return None
    voice_id = str(fields["voice_id"])
    return VoicePublic(
        voice_id=voice_id,
        voice_name=str(fields["voice_name"]),
        provider=str(fields["provider"]),
        gender=str(fields["gender"]),
        accent=str(fields["accent"]),
        age=str(fields["age"]),
        preview_audio_url=f"/voices/{voice_id}/preview",
        # Absent on some real Retell entries — absence means not-recommended,
        # per the confirmed real field behavior (never assume presence).
        recommended=bool(voice.get("recommended", False)),
    )


@router.get(
    "",
    response_model=VoiceListResponse,
    status_code=status.HTTP_200_OK,
    summary="List available voices for use as POST /agents' voice_id",
)
async def list_voices(
    caller: CurrentPlatform,
    settings: Annotated[Settings, Depends(get_settings)],
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=_MAX_LIMIT,
            description=f"Max voices to return, 1-{_MAX_LIMIT}. Default {_DEFAULT_LIMIT}.",
        ),
    ] = _DEFAULT_LIMIT,
    offset: Annotated[
        int,
        Query(ge=0, description="Number of voices to skip, for paging. Default 0."),
    ] = 0,
    provider: Annotated[
        str | None,
        Query(
            description="Filter to one vendor voice provider (e.g. 'elevenlabs', 'cartesia', "
            "'openai', 'fish_audio', 'inworld', 'minimax', 'platform'). Case-sensitive, must "
            "match exactly. Omit to include all providers."
        ),
    ] = None,
    gender: Annotated[
        str | None,
        Query(
            description="Filter to one voice gender (e.g. 'male', 'female'). Case-sensitive, "
            "must match exactly. Omit to include all genders."
        ),
    ] = None,
    recommended: Annotated[
        bool | None,
        Query(
            description="If true, only return voices the vendor marks as recommended. "
            "Omit to include both recommended and non-recommended voices."
        ),
    ] = None,
) -> VoiceListResponse:
    """List voices from the voice vendor's real voice catalog, for picking a
    `voice_id` to pass to `POST /agents`.

    Every platform sees the same catalog (it's the vendor's, not ours) — no
    tenancy scoping applies here, unlike most list endpoints in this API.
    Filtering/pagination all happen on our side after fetching the
    vendor's full list in one call, since the vendor's own endpoint has no
    filter/pagination params of its own.
    """
    del caller  # auth-only: proves a valid API key, not used for scoping

    raw_voices = await retell_adapter.list_voices(settings)

    voices = [v for raw in raw_voices if (v := _to_public(raw)) is not None]

    if provider is not None:
        voices = [v for v in voices if v.provider == provider]
    if gender is not None:
        voices = [v for v in voices if v.gender == gender]
    if recommended is not None:
        voices = [v for v in voices if v.recommended == recommended]

    total_count = len(voices)
    page = voices[offset : offset + limit]

    return VoiceListResponse(items=page, total_count=total_count, limit=limit, offset=offset)


@router.get(
    "/{voice_id}/preview",
    status_code=status.HTTP_200_OK,
    summary="Stream this voice's preview audio from our own domain",
    responses={
        200: {"content": {"audio/mpeg": {}}, "description": "Preview audio bytes."},
        404: {"description": "No voice exists with that voice_id."},
        502: {"description": "The voice vendor is unreachable or returned an error."},
    },
)
async def get_voice_preview(
    caller: CurrentPlatform,
    settings: Annotated[Settings, Depends(get_settings)],
    voice_id: Annotated[
        str,
        Path(description="The voice_id returned by GET /voices, e.g. '11labs-Adrian'."),
    ],
) -> Response:
    """Proxy this voice's preview audio through our own domain.

    `GET /voices` never exposes the vendor's own audio URL directly (see
    this module's docstring for why) — this endpoint is what
    `preview_audio_url` on each `VoicePublic` entry actually points at.
    Looks up the voice server-side, fetches the real audio bytes
    server-side (a real GET, never a redirect), and returns them directly.
    404 if `voice_id` doesn't exist in the vendor's catalog; 502 if the
    vendor is unreachable or errors — never leaks the vendor's raw
    response either way.
    """
    del caller  # auth-only: proves a valid API key, not used for scoping

    voice = await retell_adapter.get_voice(settings, voice_id=voice_id)
    audio_url = voice.get("preview_audio_url")
    if not isinstance(audio_url, str) or not audio_url:
        raise AppError(
            code=CODE_NOT_FOUND,
            message="This voice has no preview audio available.",
            status_code=404,
            field="voice_id",
        )

    audio_bytes, content_type = await retell_adapter.fetch_preview_audio(
        settings, audio_url=audio_url
    )
    return Response(content=audio_bytes, media_type=content_type)
