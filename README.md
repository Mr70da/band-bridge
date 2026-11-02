# Band Bridge

Connects a Huawei Band 11 to a Windows PC over BLE. Claude Code questions are shown on the band, and quick replies from the band go back to Claude Code. No phone, no Huawei Health, and it never touches the band's firmware.

## Setup

```
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python scan.py
```

## Credits

Protocol work is ported from [Gadgetbridge](https://codeberg.org/Freeyourgadget/Gadgetbridge) (AGPL-3.0). [huawei-lpv2](https://github.com/zyv/huawei-lpv2) is used as a structural reference.

## License

AGPL-3.0, see LICENSE.
