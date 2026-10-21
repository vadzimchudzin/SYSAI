"""Maintained SDK client example for SYSAI tools; not an OpenClaw runtime."""
import json
import os
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client



class AgentError(RuntimeError):
    pass


def validate_endpoint(url):
    parsed = urlsplit(url)
    local = (os.getenv("SYSAI_AGENT_ALLOW_LOCALHOST") == "1" and parsed.hostname in ("localhost", "127.0.0.1", "testserver"))
    if (parsed.scheme != "https" and not (local and parsed.scheme == "http")) or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AgentError("Configure a fixed HTTPS SYSAI_MCP_URL without embedded credentials")
    return url


class MCPGateway:
    def __init__(self, url=None, token=None, http_client=None):
        self.url = validate_endpoint(url or os.getenv("SYSAI_MCP_URL", ""))
        self.token = token if token is not None else os.getenv("SYSAI_MCP_TOKEN", "")
        if len(self.token) < 32:
            raise AgentError("Configure SYSAI_MCP_TOKEN")
        self.http_client = http_client

    async def __aenter__(self):
        self.owned_client = self.http_client is None
        self.http_client = self.http_client or httpx.AsyncClient(headers={"Authorization": "Bearer " + self.token},
                                                               timeout=httpx.Timeout(45))
        self.transport = streamable_http_client(self.url, http_client=self.http_client)
        read, write, _ = await self.transport.__aenter__()
        self.session_context = ClientSession(read, write)
        self.session = await self.session_context.__aenter__()
        await self.session.initialize()
        return self

    async def __aexit__(self, *exc):
        try:
            await self.session_context.__aexit__(*exc)
        finally:
            try:
                await self.transport.__aexit__(*exc)
            finally:
                if self.owned_client:
                    await self.http_client.aclose()

    async def call(self, name, arguments=None):
        result = await self.session.call_tool(name, arguments or {})
        if result.isError:
            raise AgentError("SYSAI tool rejected operation; inspect authenticated main diagnostics")
        if result.structuredContent is not None:
            return result.structuredContent
        for content in result.content:
            if content.type == "text":
                return json.loads(content.text)
        raise AgentError("Tool returned no structured result")
