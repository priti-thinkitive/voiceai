"""Voice — the public shape of one entry in Retell's real voice catalog, as
returned by `GET /voices` (see app/routers/voices.py, app/services/retell_adapter.py).

This is a read-through proxy to Retell's own `GET /list-voices`, not a synced
local collection — there is no `VoiceInDB`/Mongo document here at all, only
the public response shape. See retell_adapter.list_voices()'s docstring for
the confirmed real field set Retell returns.

Field selection is deliberate, not a raw passthrough of Retell's response
object (per the standards doc's "design from the integrating platform's
side" principle): we expose `voice_id`, `voice_name`, `provider`, `gender`,
`accent`, `age`, `preview_audio_url`, and `recommended` — the fields a
Platform X developer actually needs to browse/pick a voice and then supply
`voice_id` back on `POST /agents`. We deliberately drop Retell's
`voice_type`/`standard_voice_type` fields (Retell's own internal
categorization of how the voice is sourced/tiered — not meaningful to a
caller deciding which voice to use) and `avatar_url` (a UI icon image, not
functionally useful for an API integration with no photo UI on our side
today; easy to add later if a real UI need shows up).

`voice_id` itself is fine to expose as-is (unlike agent-creation's
`vendor_ref`) — it's not an internal correlation ID, it's the literal public
catalog identifier Platform X is meant to see, choose, and pass back on
`POST /agents`.

**`preview_audio_url` points at OUR own domain, never Retell's, as of the
fix documented in app/routers/voices.py's module docstring.** A real, live
vendor-identity leak was found and confirmed: this field originally
passed through Retell's raw S3 URL verbatim (e.g.
`https://retell-utils-public.s3.us-west-2.amazonaws.com/cartesia-....mp3`),
which puts the literal string "retell" directly in a response body any
Platform X developer can inspect — the same class of bug as a leaked
`retell_call_id` field, just via a URL instead of a field name. Fixed by
applying the exact re-hosting pattern vendor-docs/Full-System-Architecture.html
already establishes for call recordings/transcripts ("download and re-store
... on our own domain") to preview audio too: this field is now a relative
path on our own API, `/voices/{voice_id}/preview`, which proxies the real
audio bytes server-side rather than ever handing out Retell's URL.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class VoicePublic(BaseModel):
    """One voice in the voice vendor's catalog, as exposed through our own API."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "voice_id": "11labs-Adrian",
                "voice_name": "Adrian",
                "provider": "elevenlabs",
                "gender": "male",
                "accent": "American",
                "age": "Middle Aged",
                "preview_audio_url": "/voices/11labs-Adrian/preview",
                "recommended": True,
            }
        }
    )

    voice_id: str
    voice_name: str
    provider: str
    gender: str
    accent: str
    age: str
    preview_audio_url: str
    recommended: bool
