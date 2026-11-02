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

Start the bridge and keep it running (it binds to 127.0.0.1 only; see "Run at startup" below to autostart it with a tray icon):

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

The hook always exits 0, also when the bridge is not running. The band only buzzes when you have been away from the keyboard and mouse for 60 s (`BAND_BRIDGE_AWAY_AFTER`; `BAND_BRIDGE_ALWAYS=1` always sends). The title is the project folder's name, and "Task done" includes the first line of Claude's last message.

Permission prompts on the band: when you are away, Claude Code's permission dialog is asked on the band as "<project>: allow Bash? npm test" with Allow / Deny. If you come back to the PC, or there is no answer within `BAND_BRIDGE_PERMISSION_TIMEOUT` (240 s), or anything fails, the normal dialog shows. An error never allows. Add to the same `hooks`:

```json
"PermissionRequest": [{"hooks": [{"type": "command", "timeout": 300, "command": "\"C:\Users\lenovo\Desktop\Band 11\band-bridge\.venv\Scripts\python.exe\" \"C:\Users\lenovo\Desktop\Band 11\band-bridge\hooks\band_permission.py\""}]}]
```

## More from the band

- **Option text as quick replies:** `ask_on_band` takes up to 5 options and puts them on the band as the reply list, so you tap "Run tests" instead of "2". Numbers still work.
- **Music:** the band's music screen shows what the PC is playing (Windows media session). Play and pause work as usual. One tap on previous/next jumps 10 s back/forward, and a double tap changes the track (if the player has one: a YouTube playlist or Spotify, but not a single YouTube video). The volume slider sets the Windows volume.
- **Windows notifications:** new toasts are mirrored to the band while you are away. Configure in `config/band.json`: `"mirror_notifications": {"enabled": true, "only_when_away": true, "apps": [], "ignore": ["python", "pythonw", "pystray", "claude"]}` (`apps` empty = all; substring match on the app's name or id).
- **Tray:** the tooltip shows the battery and refreshes every minute, and a toast appears once when it drops below 15%.

## Run at startup (Phase 8)

`tray.py` runs the bridge and a system-tray dot in one process. Install it as a logon task for the current user (no admin rights):

```
powershell -ExecutionPolicy Bypass -File install_startup.ps1
powershell -ExecutionPolicy Bypass -File uninstall_startup.ps1
```

The task `BandBridge` starts `.venv\Scripts\pythonw.exe tray.py` at logon, restarts it up to 3 times (1 minute apart) if it fails, and has no time limit. Start it without logging out: `Start-ScheduledTask -TaskName BandBridge`.

Tray dot: green = ready, amber = connecting or waiting to retry, red = needs pairing, stopped or error (hover for the state and battery; a pairing notice says to run `connect.py`). Menu: Status (state, battery, connects/disconnects), Open log folder, Quit (closes the BLE session first).

Logs are in `logs\`: `bridge-YYYYmmdd.log` (the bridge and the tray) and `ble-*.log` (BLE packets).

Run only one of `tray.py` and `python -m bridge.api`: both use port 8765 and the band's single BLE connection.

## Credits

Protocol work is ported from [Gadgetbridge](https://codeberg.org/Freeyourgadget/Gadgetbridge) (AGPL-3.0). [huawei-lpv2](https://github.com/zyv/huawei-lpv2) is used as a structural reference.

## License

AGPL-3.0, see LICENSE.
