# Ported from Gadgetbridge (AGPL-3.0): Weather.java, HuaweiWeatherManager.java, SendWeather*Request.java,
# AsynchronousResponse.java (handleWeatherCheck)
import asyncio
import json
import logging
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from band.packet import TLV

SERVICE = 0x0F
CURRENT, SUPPORT, DEVICE, UNIT, EXTENDED, FORECAST, START, SUN_MOON = 1, 2, 4, 5, 6, 8, 9, 0x0A
OK = 0x186A0  # tag 7F success value
OK_START = (0x186A0, 0x186A3)  # 0F/09 success values
ASK = 0x186AA  # 0F/04 7F value: the band asks the phone to send weather
TIMEOUT = 5  # seconds to wait for the band's answer to one request
REFRESH = 30 * 60  # seconds between fetches
HOURS, DAYS = 24, 8  # most hourly / daily entries the band takes (today counts as a day)
CAP_UV, CAP_EXTENDED_HOURLY = 0x2F, 0xC0  # expand-capability bits, tested like HuaweiState.supportsExpandCapability
SOURCE = "Open-Meteo"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
GEOCODING = "https://geocoding-api.open-meteo.com/v1/search"

log = logging.getLogger("band.weather")

# Weather.WeatherIcon -> byte (iconToByte)
SUNNY, CLOUDY, OVERCAST, SHOWERS, THUNDERSTORMS, THUNDER_AND_HAIL, SLEET, LIGHT_RAIN, RAIN, HEAVY_RAIN = range(10)
RAIN_STORM, SEVERE_RAIN_STORMS, SNOW_FLURRIES, LIGHT_SNOW, SNOW, HEAVY_SNOW, FOG, FREEZING_RAIN = 0x0A, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x12, 0x13
UNKNOWN = 0x63  # "any higher and the current weather breaks"

# WMO weather code (Open-Meteo) -> icon, following HuaweiWeatherManager.openWeatherMapConditionCodeToHuaweiIcon
# (drizzle = OWM 3xx = light rain, 61/63/65 = OWM 500/501/502, 71/73/75 = OWM 600/601/602, 95+ = OWM 2xx).
WMO_ICON = {0: SUNNY, 1: SUNNY, 2: CLOUDY, 3: OVERCAST, 45: FOG, 48: FOG, 51: LIGHT_RAIN, 53: LIGHT_RAIN,
            55: LIGHT_RAIN, 56: FREEZING_RAIN, 57: FREEZING_RAIN, 61: LIGHT_RAIN, 63: RAIN, 65: HEAVY_RAIN,
            66: FREEZING_RAIN, 67: FREEZING_RAIN, 71: LIGHT_SNOW, 73: SNOW, 75: HEAVY_SNOW, 77: LIGHT_SNOW,
            80: SHOWERS, 81: SHOWERS, 82: HEAVY_RAIN, 85: SNOW_FLURRIES, 86: SNOW, 95: THUNDERSTORMS,
            96: THUNDER_AND_HAIL, 99: THUNDER_AND_HAIL}


def icon(wmo_code) -> int:
    return WMO_ICON.get(wmo_code, UNKNOWN)


def beaufort(kmh: float) -> int:
    for force, limit in enumerate((1, 6, 12, 20, 29, 39, 50, 62, 75, 89, 103, 118)):
        if kmh < limit:
            return force
    return 12


def wind_direction(degrees) -> int:
    """convertWindDirection: 0 none, 1 NE, 2 E, 3 SE, 4 S, 5 SW, 6 W, 7 NW, 8 N."""
    if degrees is None or not 0 <= degrees <= 360:
        return 0
    return (int(degrees / 45 + 0.5) % 8) or 8


# --- the model ------------------------------------------------------------------------------------------------------

@dataclass
class Hour:
    time: int  # unix seconds
    code: int  # WMO
    temp: int  # Celsius
    precip: int = 0  # probability %
    uv: int = 0


@dataclass
class Day:
    time: int
    code: int
    high: int
    low: int
    sunrise: int = 0
    sunset: int = 0


@dataclass
class Forecast:
    location: str
    time: int
    code: int
    temp: int  # always Celsius: Gadgetbridge sends Celsius and lets the unit flag (tag 0A / 0F/05) pick the display
    feels: int
    humidity: int
    wind_kmh: float
    wind_dir: int
    uv: float = 0
    hourly: list = field(default_factory=list)
    days: list = field(default_factory=list)  # days[0] is today


def _round(x) -> int:
    return int(round(x or 0))


def parse_open_meteo(data: dict, location: str) -> Forecast:
    """Open-Meteo JSON (timeformat=unixtime) -> Forecast. Raises KeyError/ValueError on a malformed answer."""
    c, h, d = data["current"], data["hourly"], data["daily"]
    now = int(c["time"])
    hours = [Hour(t, int(code), _round(temp), _round(p), _round(uv)) for t, code, temp, p, uv in zip(
        h["time"], h["weather_code"], h["temperature_2m"], h["precipitation_probability"], h["uv_index"])
        if t > now - 3600 and None not in (code, temp)][:HOURS]
    days = [Day(t, int(code), _round(hi), _round(lo), sr or 0, ss or 0) for t, code, hi, lo, sr, ss in zip(
        d["time"], d["weather_code"], d["temperature_2m_max"], d["temperature_2m_min"], d["sunrise"], d["sunset"])
        if None not in (code, hi, lo)][:DAYS]
    if not days:
        raise ValueError("no daily forecast")
    uv = max((x.uv for x in hours[:12]), default=0)
    return Forecast(location, now, int(c["weather_code"]), _round(c["temperature_2m"]),
                    _round(c.get("apparent_temperature", c["temperature_2m"])), _round(c.get("relative_humidity_2m")),
                    float(c.get("wind_speed_10m") or 0), _round(c.get("wind_direction_10m")), uv, hours, days)


class OpenMeteo:
    """The real fetcher: fetch(config) -> Forecast, blocking (the service runs it in a thread). The geocoding of
    {"city": ...} happens once per city."""

    def __init__(self, urlopen=urllib.request.urlopen):
        self._urlopen = urlopen
        self._places = {}

    def _get(self, base, params):
        url = f"{base}?{urllib.parse.urlencode(params)}"
        with self._urlopen(urllib.request.Request(url, headers={"User-Agent": "band-bridge"}), timeout=20) as r:
            return json.load(r)

    def place(self, config) -> tuple[float, float, str]:
        if "latitude" in config and "longitude" in config:
            return float(config["latitude"]), float(config["longitude"]), str(config.get("name") or "")
        city = config["city"]
        if city not in self._places:
            res = self._get(GEOCODING, {"name": city, "count": 1}).get("results")
            if not res:
                raise ValueError(f"city not found: {city}")
            self._places[city] = (res[0]["latitude"], res[0]["longitude"], config.get("name") or res[0]["name"])
        return self._places[city]

    def __call__(self, config) -> Forecast:
        lat, lon, name = self.place(config)
        data = self._get(OPEN_METEO, {
            "latitude": lat, "longitude": lon, "timezone": "auto", "timeformat": "unixtime", "forecast_days": 7,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,"
                       "wind_direction_10m",
            "hourly": "temperature_2m,weather_code,precipitation_probability,uv_index",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset"})
        return parse_open_meteo(data, name)


# --- the packets ----------------------------------------------------------------------------------------------------

@dataclass
class Settings:
    """Weather.Settings: what the band says it can show."""
    wind: bool = False
    pm25: bool = False
    temperature: bool = False  # low/high
    location_name: bool = False
    current_temperature: bool = False
    unit: bool = False
    aqi: bool = False
    time: bool = False
    source: bool = False
    icon: bool = False
    sun_rise_set: bool = False
    moon_phase: bool = False
    uv: bool = False  # expand capability 0x2F
    extended_hourly: bool = False  # expand capability 0xC0


def _i8(n) -> bytes:
    return (int(n) & 0xFF).to_bytes(1, "big")  # temperatures are signed bytes


def has_capability(expand_caps_hex, which) -> bool:
    try:
        caps = bytes.fromhex(expand_caps_hex or "")
    except ValueError:
        return False
    return which // 8 < len(caps) and bool(caps[which // 8] & (1 << which % 8))


def parse_support(tlv: TLV) -> dict:
    """0F/02 response, tag 01 = bitmap (WeatherSupport.Response)."""
    b = tlv.get_int(1, 0)
    return dict(zip(("wind", "pm25", "temperature", "location_name", "current_temperature", "unit", "aqi"),
                    (bool(b >> n & 1) for n in range(1, 8))))


def start_tlv() -> TLV:
    return TLV().put_u8(1, 3)  # 0F/09, "TODO: find out what this means" in Gadgetbridge


def unit_tlv(fahrenheit: bool) -> TLV:
    return TLV().put_u8(1, int(fahrenheit))


def device_ack() -> TLV:
    return TLV().put_u32(1, OK)  # 0F/04 phone -> band, not encrypted


def current_tlv(f: Forecast, s: Settings, fahrenheit=False) -> TLV:
    """0F/01 CurrentWeatherRequest. Optional tags only when the band said it shows them."""
    t, today = TLV(), f.days[0]
    inner = TLV()
    if s.icon:
        inner.put_u8(2, icon(f.code))
    if s.wind:
        inner.put_u16(3, wind_direction(f.wind_dir) << 8 | beaufort(f.wind_kmh))
    if s.icon or s.wind:
        t.put(0x81, inner)
    if s.temperature:
        t.put(0x85, TLV().put(6, _i8(today.low)).put(7, _i8(today.high)))
    if s.location_name and f.location:
        t.put(8, f.location)
    if s.current_temperature:
        t.put(9, _i8(f.temp))
    if s.unit:
        t.put_u8(0x0A, int(fahrenheit))
    if s.time:
        t.put_u32(0x0C, f.time)
    if s.source:
        t.put(0x0E, SOURCE)
    if s.uv:
        t.put_u8(0x0F, _round(f.uv))
    if s.extended_hourly:
        t.put_u8(0x10, f.humidity).put_u32(0x11, _round(f.wind_kmh)).put_u32(0x12, f.feels & 0xFFFFFFFF)
    return t


def _in_day(ts, day_start) -> bool:
    return day_start <= ts < day_start + 86400


def forecast_tlv(f: Forecast, s: Settings) -> TLV:
    """0F/08 WeatherForecastData.Request (sliced by Session.send). Sun times outside their UTC day are dropped
    (fixupWeather: wrong ones make the band show no weather at all)."""
    t = TLV()
    if f.hourly:
        hours = TLV()
        for h in f.hourly:
            e = TLV().put_u32(3, h.time)
            if s.icon:
                e.put_u8(4, icon(h.code))
            if s.temperature or s.current_temperature:
                e.put(5, _i8(h.temp))
            if s.extended_hourly:
                e.put_u8(6, h.precip).put_u8(7, h.uv)
            hours.put(0x82, e)
        t.put(0x81, hours)
    days, day0 = TLV(), f.time // 86400 * 86400
    for i, d in enumerate(f.days):
        e = TLV().put_u32(0x12, f.time + i * 86400)  # Gadgetbridge: now + i days
        if s.icon:
            e.put_u8(0x13, icon(d.code))
        if s.temperature:
            e.put(0x14, _i8(d.high)).put(0x15, _i8(d.low))
        if s.sun_rise_set:
            start = day0 + i * 86400
            if d.sunrise and _in_day(d.sunrise, start):
                e.put_u32(0x16, d.sunrise)
            if d.sunset and _in_day(d.sunset, start):
                e.put_u32(0x17, d.sunset)
        days.put(0x91, e)
    return t.put(0x90, days)


# --- the service ----------------------------------------------------------------------------------------------------

class WeatherService:
    """Survives reconnects: setup(session) on every new Session; run() is the one long-lived refresher."""

    def __init__(self, config=None, fetch=None):
        self.config = config if isinstance(config, dict) and (
            "city" in config or ("latitude" in config and "longitude" in config)) else None
        self.fahrenheit = bool(self.config) and str(self.config.get("units", "")).lower() == "imperial"
        self.fetch = fetch or OpenMeteo()
        self.latest = None
        self._session = None
        self._lock = asyncio.Lock()  # one 0F sequence at a time (Session.request allows one per command)
        self._tasks = set()
        self._warned = False

    def _log(self, text, session=None):
        session = session or self._session
        if session is not None:
            session.transport.log("EVT", f"weather {text}")
        else:
            log.warning(text)

    def _live(self):
        s = self._session
        return s is not None and not s.disconnected.is_set()

    async def refresh(self) -> bool:
        """Fetch in a thread; keep the old data on failure."""
        try:
            self.latest = await asyncio.to_thread(self.fetch, self.config)
            return True
        except Exception as e:
            self._log(f"fetch failed: {e!r}")
            return False

    def _enabled(self) -> bool:
        if self.config:
            return True
        if not self._warned:
            self._warned = True
            self._log("not configured (config key 'weather': latitude+longitude or city), service idle")
        return False

    async def setup(self, session):
        self._session = session
        if not self._enabled():
            return
        if self.latest is None:
            await self.refresh()
        await self.push(session)

    async def _ask(self, session, cmd, tlv):
        return (await session.request(SERVICE, cmd, tlv, timeout=TIMEOUT)).tlv

    async def push(self, session=None):
        """Send the latest weather: start, unit, capability queries, current, forecast. Failures are logged,
        never raised."""
        session = session or self._session
        f = self.latest
        if f is None or session is None or session.disconnected.is_set():
            return
        try:
            async with self._lock:
                await self._send(session, f)
        except Exception as e:
            self._log(f"push failed: {e!r}", session)

    async def _send(self, session, f):
        t = await self._ask(session, START, start_tlv())
        if t.get_int(0x7F, -1) not in OK_START:
            return self._log(f"0F/09 refused, 7F={t.get_int(0x7F, -1):X}", session)
        s = Settings()
        caps = session.config.get("expand_caps")
        s.uv, s.extended_hourly = has_capability(caps, CAP_UV), has_capability(caps, CAP_EXTENDED_HOURLY)
        await self._optional(session, UNIT, unit_tlv(self.fahrenheit))
        sup = await self._optional(session, SUPPORT, TLV().put(1))
        if sup is None or 1 not in sup:
            return self._log("0F/02 gave no capability bitmap, nothing sent", session)
        for k, v in parse_support(sup).items():
            setattr(s, k, v)
        ext = await self._optional(session, EXTENDED, TLV().put(1))
        if ext is not None and 1 in ext:
            b = ext.get_int(1)
            s.time, s.source, s.icon = bool(b & 1), bool(b & 2), bool(b & 4)
        sun = await self._optional(session, SUN_MOON, TLV().put(1))
        if sun is not None and 1 in sun:
            b = sun.get_int(1)
            s.sun_rise_set, s.moon_phase = bool(b & 1), bool(b & 2)
        await self._optional(session, CURRENT, current_tlv(f, s, self.fahrenheit))
        await self._optional(session, FORECAST, forecast_tlv(f, s))

    async def _optional(self, session, cmd, tlv):
        """The band's answer, or None (logged) when it didn't answer: not every band has every command."""
        try:
            t = await self._ask(session, cmd, tlv)
        except (TimeoutError, ConnectionError) as e:
            self._log(f"0F/{cmd:02X}: {e}", session)
            return None
        if t.get_int(0x7F, OK) not in (OK, *OK_START):
            self._log(f"0F/{cmd:02X} rejected, 7F={t.get_int(0x7F):X}", session)
        return t

    async def run(self):
        """Refresh every REFRESH seconds and push to the live session."""
        while True:
            try:
                await asyncio.sleep(REFRESH)
                if self._enabled() and await self.refresh() and self._live():
                    await self.push(self._session)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log(f"refresh failed: {e!r}")

    def on_message(self, msg):
        if msg.service != SERVICE or self._session is None:
            return
        if msg.command != DEVICE:
            self._log(f"0F/{msg.command:02X} from the band, ignored: {msg.tlv.serialize().hex() if msg.tlv else ''}")
            return
        task = asyncio.get_running_loop().create_task(self._handle(self._session, msg))
        self._tasks.add(task)  # the loop only keeps weak references
        task.add_done_callback(self._tasks.discard)

    async def _handle(self, session, msg):
        """0F/04: with 7F=0x186AA the band asks for weather (Gadgetbridge sends it); anything else is acked
        (unencrypted, like Gadgetbridge's SendWeatherDeviceRequest)."""
        try:
            if msg.tlv is not None and msg.tlv.get_int(0x7F, -1) == ASK:
                if self._enabled():
                    if self.latest is None:
                        await self.refresh()
                    await self.push(session)
                return
            await session.send(SERVICE, DEVICE, device_ack(), encrypt=False)
        except Exception as e:
            self._log(f"0F/04 handling failed: {e!r}", session)
