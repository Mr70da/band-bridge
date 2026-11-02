"""MCP stdio server for Claude Code. Needs only the stdlib and mcp; talks to bridge.api over localhost."""
import asyncio
import json
import os
import urllib.error
import urllib.request

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("band-bridge")


def base_url() -> str:
    return f"http://127.0.0.1:{os.environ.get('BAND_BRIDGE_PORT', '8765')}"


def _post(path: str, payload: dict, timeout: float) -> tuple[int, dict]:
    req = urllib.request.Request(base_url() + path, json.dumps(payload).encode(),
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except ValueError:
            return e.code, {}


async def call(path: str, payload: dict, timeout: float) -> tuple[int, dict] | str:
    """(status, body), or a message for the model when the bridge cannot be reached. Never raises."""
    try:
        return await asyncio.to_thread(_post, path, payload, timeout)
    except TimeoutError:
        return "The band bridge did not answer in time."
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):  # connect phase
            return "The band bridge did not answer in time."
        return "The band bridge is not running. Ask the user to start it: python -m bridge.api"
    except (OSError, ValueError) as e:  # includes socket timeouts and bad JSON
        return f"The band bridge failed: {e!r}"


def failure(status: int, body: dict) -> str:
    return f"Band bridge error ({status}): {body.get('error', 'unknown error')}"


@mcp.tool()
async def ask_on_band(question: str, options: list[str] = [], timeout_seconds: int = 300) -> str:
    """Ask the user a short question on their Huawei Band and wait for their tap. Use for choices with up to 5 options (the user taps the option text on the band; keep each option under 30 characters), or yes/no/continue/stop questions. Keep the question short (it is shown on a small screen)."""
    result = await call("/ask", {"question": question, "options": options, "timeout": timeout_seconds},
                        timeout_seconds + 15)
    if isinstance(result, str):
        return result
    status, body = result
    if status != 200:
        return failure(status, body)
    option = f" (option {body['option']})" if body.get("option") else ""
    return f'User answered on the band: "{body["answer"]}"{option}'


@mcp.tool()
async def notify_band(text: str) -> str:
    """Show a short one-way message on the user's Huawei Band (no reply expected)."""
    result = await call("/notify", {"text": text}, 20)
    if isinstance(result, str):
        return result
    status, body = result
    return "Sent to the band." if status == 200 else failure(status, body)


if __name__ == "__main__":
    mcp.run()
