"""Issue a new platform + API key. The only way to get a usable key —
there is no "create platform" HTTP endpoint yet (platform onboarding is an
internal/admin operation, not exposed to callers at bootstrap time).

Usage (from backend/, with the venv activated):

    python scripts/seed_platform.py "Acme Voice Co"

Prints the plaintext API key ONCE. It is not recoverable afterwards — only
its hash is stored. If it's lost, revoke the platform and re-run this script
to issue a new one.
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")

from app.config import get_settings  # noqa: E402
from app.database import lifespan_mongo  # noqa: E402
from app.repositories import platform_repo  # noqa: E402
from app.security import generate_api_key, hash_api_key, key_display_prefix  # noqa: E402


async def main(name: str) -> None:
    settings = get_settings()
    async with lifespan_mongo(settings) as db:
        api_key = generate_api_key()
        platform = await platform_repo.create(
            db,
            name=name,
            api_key_hash=hash_api_key(api_key),
            api_key_prefix=key_display_prefix(api_key),
        )
        print("Platform created.")
        print(f"  platform_id: {platform.id}")
        print(f"  name:        {platform.name}")
        print()
        print("API key (shown once — store it now, it cannot be retrieved again):")
        print(f"  {api_key}")
        print()
        print("Test it with:")
        print(f'  curl -H "Authorization: Bearer {api_key}" ' f"{settings.BASE_URL}/_debug/whoami")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/seed_platform.py <platform-name>", file=sys.stderr)
        raise SystemExit(1)
    asyncio.run(main(sys.argv[1]))
