# Contributing

Thanks for helping. Band Bridge talks to real hardware, so small, verified changes are best.

## Setup

```
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

## Tests

```
.venv\Scripts\python -m pytest tests
```
CI runs the unit tests on windows-latest. They need no band.

## Guidelines

- Prefer changes verified on a real band, and say in the PR which band and firmware you used.
- Protocol findings go in `docs/protocol.md`. Tag each claim `[src]` (read from Gadgetbridge source) or `[log]` (seen in a real capture), and keep it free of personal data.
- Never commit `config/band.json`, `logs/` or `data/`. They hold your pairing key and personal data. Use `config/band.example.json` for documenting config keys.
- No personal paths or secrets in code, docs or tests.
- Keep the bridge local (127.0.0.1 only) and never touch firmware.

## License

By contributing you agree your work is licensed under AGPL-3.0, like the rest of the project.
