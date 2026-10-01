#!/usr/bin/env python3
"""Paid-API-free subprocess smoke test using the official MCP Python client."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

from mcp import Client, StdioServerParameters


async def main() -> None:
    api_dir = Path(__file__).resolve().parents[1] / "apps" / "api"
    with tempfile.TemporaryDirectory(prefix="allernav-mcp-") as directory:
        env = dict(os.environ)
        # Empty values prevent dotenv from re-enabling production storage or tracing.
        env.update({key: "" for key in (
            "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "LANGSMITH_API_KEY",
            "GOOGLE_MAPS_API_KEY", "GOOGLE_PLACES_API_KEY", "NEXT_PUBLIC_GOOGLE_MAPS_API_KEY",
            "AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_API_KEY",
        )})
        env.update(ALLERNAV_MENU_DB=str(Path(directory) / "menus.sqlite"), LANGSMITH_TRACING="false")
        parameters = StdioServerParameters(
            command=sys.executable, args=["-m", "allernav_api.mcp.server"], cwd=api_dir, env=env,
        )
        async with Client(parameters) as client:
            listing = await client.list_tools()
            print("Tools:", ", ".join(tool.name for tool in listing.tools))
            result = await client.call_tool("get_restaurant_menu", {"restaurant_id": "mcp-smoke-missing"})
            if result.is_error or result.structured_content["status"] != "missing":
                raise RuntimeError("MCP smoke test failed")
            print(json.dumps(result.structured_content, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
