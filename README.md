# Band Bridge

Connects a Huawei Band 11 to a Windows PC over BLE. Claude Code questions are shown on the band, and quick replies from the band go back to Claude Code. No phone, no Huawei Health, and it never touches the band's firmware.

## Setup

```
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python scan.py
```

Probe the band (Phase 2): `.venv\Scripts\python probe.py`

## Use with Claude Code (Windows)

Start the bridge and keep it running (it binds to 127.0.0.1 only; Phase 8 will autostart it):

```
.venv\Scripts\python -m bridge.api
```

Register the MCP server (tools `ask_on_band` and `notify_band`):

```
claude mcp add band-bridge --scope user -- "C:\Users\lenovo\Desktop\Band 11\band-bridge\.venv\Scripts\python.exe" "C:\Users\lenovo\Desktop\Band 11\band-bridge\bridge\mcp_server.py"
```

or the same in `~/.claude.json` / `.mcp.json`:

```json
{
  "mcpServers": {
    "band-bridge": {
      "command": "C:\Users\lenovo\Desktop\Band 11\band-bridge\.venv\Scripts\python.exe",
      "args": ["C:\Users\lenovo\Desktop\Band 11\band-bridge\bridge\mcp_server.py"]
    }
  }
}
```

An ask can wait minutes for a tap. Claude Code cuts MCP tool calls off at its own limit, so start it with a larger one, e.g. `set MCP_TOOL_TIMEOUT=600000` (milliseconds). A non-default bridge port is set with `BAND_BRIDGE_PORT` for both the bridge and the MCP server.

Hooks: a band notification when Claude needs input or is done. Put this in `%USERPROFILE%\.claude\settings.json`:

```json
{
  "hooks": {
    "Notification": [{"hooks": [{"type": "command", "command": "\"C:\Users\lenovo\Desktop\Band 11\band-bridge\.venv\Scripts\python.exe\" \"C:\Users\lenovo\Desktop\Band 11\band-bridge\hooks\band_notify.py\" input"}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "\"C:\Users\lenovo\Desktop\Band 11\band-bridge\.venv\Scripts\python.exe\" \"C:\Users\lenovo\Desktop\Band 11\band-bridge\hooks\band_notify.py\" done"}]}]
  }
}
```

The hook always exits 0, also when the bridge is not running.

## Credits

Protocol work is ported from [Gadgetbridge](https://codeberg.org/Freeyourgadget/Gadgetbridge) (AGPL-3.0). [huawei-lpv2](https://github.com/zyv/huawei-lpv2) is used as a structural reference.

## License

AGPL-3.0, see LICENSE.
