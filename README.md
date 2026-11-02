# Band Bridge

Band Bridge connects a Huawei Band 11 directly to a Windows PC over Bluetooth LE, with no phone and no Huawei Health. Its main job is to put Claude Code on your wrist: questions, permission prompts and "what next?" choices appear on the band, and a tap sends the answer back. It also turns the band into a PC accessory (notifications, music remote, lock when you leave) and syncs your health data into a local dashboard.

## Features

**Claude Code on your wrist**
- `ask_on_band`: Claude asks a question on the band; the options you give become the reply list, so you tap "Run tests" instead of "2" (up to 5 options; numbers still work).
- Permission prompts: when you are away, Claude Code's permission dialog is asked on the band with Allow / Deny. An error never allows.
- Wrist remote: when Claude finishes while you are away, the band asks "Next?" (Keep going / Run tests / Commit / Stop) and Claude continues with your choice.
- Stuck detector: after 3 failed commands in a row while you are away, the band asks Keep going / Try another way / Stop.
- Danger guard: force-push, hard reset, recursive deletes, DROP/TRUNCATE, terraform destroy and similar commands need an Allow on the band, even when you are at the PC. Fails closed.
- Session board: one band notification lists every running Claude session (waiting, done, working).

**Band as a PC accessory**
- Windows notifications mirrored to the band while you are away.
- Music controls for whatever the PC is playing: play/pause, 10 s seek on previous/next, double tap for track change, volume slider.
- Current weather (Open-Meteo, no API key).
- Lock the PC when you walk away with the band.
- System tray icon with connection state and battery.

**Health dashboard**
- Steps, calories, distance, heart rate, sleep, SpO2 and stress, synced every 15 minutes into a local SQLite file.
- Web dashboard plus an always-on-top desktop widget.

## Status and tested hardware

Tested on: Huawei Band 11, firmware 6.0.0.130, Windows 11, a standard Bluetooth LE adapter. Other Huawei bands may work but are untested.

Known limits:
- The band rejects the weather forecast, so only current weather is shown.
- Sleep, SpO2 and stress only appear if the band recorded them (wear it, enable the measurements on the band).
- Heart rate on demand is not possible; you get the values the band records.

Protocol details and evidence levels are in [docs/protocol.md](docs/protocol.md).

## Safety

- It never flashes or modifies firmware. It only sends BLE application-level messages, as the Huawei phone app does.
- Everything is local: the bridge listens on `127.0.0.1` only. The only outside request is the weather lookup.
- The pairing key is stored in `config/band.json`, which is gitignored. Never share or commit that file.

## Requirements

- Windows 10/11 with a Bluetooth LE adapter
- Python 3.13 (`py` launcher)
- Claude Code (for the Claude features)
- A Huawei Band 11, not currently connected to a phone (a band connects to one host at a time)

## Install

```
git clone <this repository's URL>
cd band-bridge
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

All commands below run from the repository root.

## Pair

1. Put the band in pairing mode and disconnect/unpair it from any phone or Huawei Health (the band talks to one host at a time). Keep it next to the PC.
2. Find it:
   ```
   .venv\Scripts\python tools\scan.py
   ```
   It lists nearby devices and highlights `HUAWEI Band 11-XXX`.
3. Pair:
   ```
   .venv\Scripts\python tools\connect.py
   ```
   When the band asks, tap the check mark on it. The key is saved to `config/band.json` (see `config/band.example.json` for the layout; `tools/connect.py` writes the real file, you do not create it by hand).

Expected: about 30 seconds after the first pairing the connection drops once. This is normal Bluetooth behavior; the bridge then reconnects with the saved key and stays connected.

## Run

Tray app (recommended):
```
.venv\Scripts\pythonw.exe tray.py
```
Start it automatically at logon (current user, no admin rights):
```
powershell -ExecutionPolicy Bypass -File install_startup.ps1
powershell -ExecutionPolicy Bypass -File uninstall_startup.ps1
```
The `BandBridge` task restarts the tray up to 3 times if it fails. Start it without logging out: `Start-ScheduledTask -TaskName BandBridge`.

Console mode (logs on screen):
```
.venv\Scripts\python -m bridge.api
```

Run only one of the two: both use port 8765 and the band's single BLE connection.

Tray dot: green = ready, amber = connecting or retrying, red = needs pairing, stopped or error. Menu: Status, Open dashboard, Show widget, Open log folder, Quit. The tooltip shows the battery, and a toast appears once when it drops below 15%.

## Use with Claude Code

```
.venv\Scripts\python setup_claude.py
```

This registers, in `%USERPROFILE%\.claude\settings.json` and for this checkout:
- the `band-bridge` MCP server (tools `ask_on_band` and `notify_band`),
- the hooks: Notification and Stop (`band_notify.py`: notifications and wrist remote), PermissionRequest (`band_permission.py`), PostToolUse/PostToolUseFailure (`band_stuck.py`), PreToolUse (`band_guard.py`) and the session board (`band_session.py`).

It backs up your settings first and is safe to run again. Options: `--dry-run` (show what would change), `--uninstall` (remove everything it added), `--no-guard` (skip the danger guard).

Then:
- Restart Claude Code.
- An ask can wait minutes for a tap, but Claude Code cuts MCP tool calls off at its own limit. Start it with a larger one, for example `set MCP_TOOL_TIMEOUT=600000` (milliseconds).
- The band only buzzes when you have been away from keyboard and mouse for 60 s. Hooks always exit 0 when the bridge is down, except the guard, which fails closed.
- Quoted command text such as a commit message also trips the guard; use `git commit -F file` to avoid that.

## Configuration

`config/band.json` (all keys optional except what pairing writes):

| Key | Default | Meaning |
|---|---|---|
| `quick_replies` | `["1","2","3","yes","no","continue","stop"]` | Reply list shown for notifications without options |
| `weather` | none (weather off) | `{"latitude": 24.4672, "longitude": 39.6111, "name": "Madinah"}` or `{"city": "Cairo"}`; add `"units": "imperial"` for Fahrenheit |
| `mirror_notifications` | `{"enabled": true, "only_when_away": true, "apps": [], "ignore": ["python","pythonw","pystray","claude"]}` | Windows toast mirroring; `apps` empty = all, substring match on app name or id |
| `lock_on_leave` | `{"enabled": true, "after": 60}` | Lock after the band has been out of range and the PC idle for `after` seconds |
| `fitness` | `{"enabled": true, "interval": 900, "delay": 5, "days": 7}` | Sync on/off, seconds between syncs, seconds after connect, days fetched on first run |
| `steps_goal` | `8000` | Daily step goal in the dashboard |

Environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `BAND_BRIDGE_PORT` | `8765` | Local port; set it for the bridge, hooks and MCP server alike |
| `BAND_BRIDGE_AWAY_AFTER` | `60` | Seconds of no input before you count as away |
| `BAND_BRIDGE_ALWAYS` | unset | `1` = always use the band, even when you are at the PC |
| `BAND_BRIDGE_REMOTE` | on | `0` = plain "Task done" instead of the wrist remote |
| `BAND_BRIDGE_GUARD` | on | `0` = disable the danger guard |
| `BAND_BRIDGE_PERMISSION_TIMEOUT` | `240` | Seconds to wait for a permission answer, then the normal dialog shows |
| `BAND_BRIDGE_REMOTE_TIMEOUT` | `240` | Seconds to wait for the "Next?" answer |
| `BAND_BRIDGE_GUARD_TIMEOUT` | `120` | Seconds to wait for the guard; no answer blocks the command |

## Dashboard and widget

Open `http://127.0.0.1:8765/dashboard` (tray: Open dashboard) or the desktop widget (tray: Show widget). Data lives in `data/fitness.db`. Try them without a band:
```
.venv\Scripts\python -m bridge.dashboard --demo --port 8799
.venv\Scripts\python widget.py --demo
```
`GET /sessions` shows the Claude session board at any time.

## Troubleshooting

- **Band not found**: make sure it is not connected to a phone, is close to the PC, and that Bluetooth is on. Run `tools\scan.py` to see if it advertises.
- **State `needs_pairing` (red tray dot)**: the band rejected the stored key (for example after a reset or after pairing with a phone). Put the band in pairing mode and run `tools\connect.py` again.
- **Port busy**: another instance (tray or console) is running, or something else uses 8765. Quit it or set `BAND_BRIDGE_PORT`.
- **Logs**: `logs\bridge-YYYYmmdd.log` (bridge and tray) and `logs\ble-*.log` (BLE packets). Tray: Open log folder.
- **Unpairing / starting over**: quit the bridge, delete `config/band.json`, remove the band from the phone app if needed, then pair again.

## Development

```
.venv\Scripts\python -m pytest tests
```
Dev scripts are in `tools/`: `scan.py`, `connect.py`, `probe.py`, `soak.py`, `notify_test.py`, `fitness_probe.py` (run one sync by hand; stop the tray first). Protocol notes live in [docs/protocol.md](docs/protocol.md). See [CONTRIBUTING.md](CONTRIBUTING.md).

## Credits

Protocol work is ported from [Gadgetbridge](https://codeberg.org/Freeyourgadget/Gadgetbridge) (AGPL-3.0), including its logs of Band 11 pairing. [huawei-lpv2](https://github.com/zyv/huawei-lpv2) was used as a structural reference only.

## License

AGPL-3.0, see [LICENSE](LICENSE). Code is ported from Gadgetbridge, which is AGPL-3.0 too.

## Disclaimer

Band Bridge is not affiliated with or endorsed by Huawei. Use it at your own risk.
