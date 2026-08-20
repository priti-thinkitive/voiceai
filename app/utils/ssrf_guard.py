"""SSRF-adjacent guard for any URL Platform X registers that WE later make a
real outbound HTTP request to on their behalf, automatically, with no human
review in between.

Extracted from app/routers/platform.py (originally built for
`inbound_variables_webhook_url`/`call_completed_webhook_url`) into this
shared module so a second call site — a custom tool's own `webhook_url` (see
app/models/agent.py's `CustomToolDefinition`) — can reuse the identical
check rather than re-implementing it or importing a router's private
function across modules. `platform.py` itself is updated to call this
shared version too, so there is exactly one implementation, not two that can
drift apart.

**The real reasoning (unchanged from the original)**: a registered URL is
not just stored data — it is an address our own server later POSTs to
automatically. A malicious or misconfigured Platform X could register a URL
pointing at our own internal infrastructure (a loopback/private-network
service with no auth of its own) and use us as a confused-deputy proxy into
our own network. Resolves the hostname via real DNS (`socket.getaddrinfo`)
rather than a string match against "localhost"/"127.0.0.1" — a string check
alone would miss `http://some-internal-name/` resolving to a private IP via
internal DNS. Checked at registration time only — closing a full
DNS-rebinding gap (resolve public at registration, reroute to private at
call time) would need a per-relay resolution check at the moment of every
outbound call, which is a larger change than any of this project's
registration endpoints are scoped to do — a known, named residual gap, not
silently assumed away.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket

from fastapi import status
from pydantic import HttpUrl

from app.errors import CODE_VALIDATION, AppError


def _resolves_to_internal_host(url: HttpUrl) -> bool:
    """Real, synchronous DNS resolution — blocking I/O, so this must always
    be called via `asyncio.to_thread` from an `async def`, never awaited
    directly (a single event loop serves every concurrent request; a
    blocking DNS lookup here would stall every other in-flight request too).
    """
    hostname = url.host
    if not hostname:
        return True
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return True
    for info in infos:
        addr = info[4][0]
        ip = ipaddress.ip_address(addr)
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified:
            return True
    return False


async def reject_if_internal_url(url: HttpUrl | None, *, field: str) -> None:
    """Raise AppError(422) if `url` resolves to an internal/private network
    address. A no-op if `url` is None (nothing to check — clearing a field,
    or an optional field left unset).
    """
    if url is None:
        return
    is_internal = await asyncio.to_thread(_resolves_to_internal_host, url)
    if is_internal:
        raise AppError(
            code=CODE_VALIDATION,
            message="This URL resolves to an internal/private network address, which is "
            "not allowed for a webhook URL we call on your behalf.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            field=field,
        )
