import asyncio
import io
import json
import unittest
from unittest import mock

from band.packet import TLV, Message
from band.services import weather as w
from band.services.weather import Day, Forecast, Hour, Settings, WeatherService

NOW = 1_793_645_100
FULL = Settings(wind=True, pm25=True, temperature=True, location_name=True, current_temperature=True, unit=True,
                aqi=True, time=True, source=True, icon=True, sun_rise_set=True, uv=True, extended_hourly=True)


def forecast(**kw) -> Forecast:
    day0 = NOW // 86400 * 86400
    f = Forecast("Cairo", NOW, 2, 24, -3, 40, 3.2, 207, 5,
                 [Hour(NOW + 3600, 0, 21, 10, 2), Hour(NOW + 7200, 61, -2, 80, 0)],
                 [Day(day0, 2, 32, 19, day0 + 3600, day0 + 40000), Day(day0 + 86400, 3, 30, 18)])
    for k, v in kw.items():
        setattr(f, k, v)
    return f


def msg(cmd, tlv, svc=0x0F) -> Message:
    return Message(svc, cmd, tlv.serialize(), tlv)


class FakeSession:
    def __init__(self, bitmap=0xFF, ext=7, sun=1, caps="FF7F"):
        self.requests, self.sent, self.logs = [], [], []
        self.disconnected = asyncio.Event()
        self.config = {"expand_caps": caps}
        self.replies = {w.START: TLV().put_u32(0x7F, w.OK), w.SUPPORT: TLV().put_u8(1, bitmap),
                        w.EXTENDED: TLV().put_u16(1, ext), w.SUN_MOON: TLV().put_u32(1, sun)}
        self.errors = {}
        self.transport = mock.Mock(log=lambda way, text: self.logs.append(text))

    async def request(self, svc, cmd, tlv, timeout=10):
        if cmd in self.errors:
            raise self.errors[cmd]
        self.requests.append((svc, cmd, tlv.serialize()))
        return msg(cmd, self.replies.get(cmd, TLV().put_u32(0x7F, w.OK)))

    async def send(self, svc, cmd, tlv, encrypt=True):
        self.sent.append((svc, cmd, tlv.serialize(), encrypt))


class PacketTest(unittest.TestCase):
    def test_start_unit_ack(self):
        self.assertEqual(w.start_tlv().serialize(), b"\x01\x01\x03")
        self.assertEqual(w.unit_tlv(False).serialize(), b"\x01\x01\x00")
        self.assertEqual(w.unit_tlv(True).serialize(), b"\x01\x01\x01")
        self.assertEqual(w.device_ack().serialize(), b"\x01\x04\x00\x01\x86\xa0")

    def test_support_bitmap(self):
        # bit 0 weather, 1 wind, 2 pm25, 3 temperature, 4 location, 5 current temperature, 6 unit, 7 aqi
        self.assertTrue(all(w.parse_support(TLV().put_u8(1, 0xFF)).values()))
        got = w.parse_support(TLV().put_u8(1, 0x23))
        self.assertEqual([k for k, v in got.items() if v], ["wind", "current_temperature"])

    def test_capability_bits(self):
        caps = "FF7F014206C37442A0A6BC7222698A4B87AAA10D6EFDCC8A8F511901594DA48A0A42100000"
        self.assertTrue(w.has_capability(caps, 0x2F))   # byte 5 = 0x42... bit 7
        self.assertFalse(w.has_capability("", 0xC0))
        self.assertFalse(w.has_capability("zz", 1))
        self.assertTrue(w.has_capability("0100", 0) and not w.has_capability("0100", 1))

    def test_current_all_supported(self):
        # 81{02 icon 02(cloudy=1), 03 wind (dir 207 deg -> SW=5)<<8 | beaufort 1}, 85{06 low, 07 high}, 08 name,
        # 09 temp, 0A unit, 0C time, 0E source, 0F uv, 10 humidity, 11 wind, 12 feels like (int, signed)
        got = w.current_tlv(forecast(), FULL).serialize()
        want = (b"\x81\x07" b"\x02\x01\x01" b"\x03\x02\x05\x01"
                b"\x85\x06" b"\x06\x01\x13" b"\x07\x01\x20"
                b"\x08\x05Cairo" b"\x09\x01\x18" b"\x0a\x01\x00" b"\x0c\x04" + NOW.to_bytes(4, "big") +
                b"\x0e\x0aOpen-Meteo" b"\x0f\x01\x05" b"\x10\x01\x28" b"\x11\x04\x00\x00\x00\x03"
                b"\x12\x04\xff\xff\xff\xfd")
        self.assertEqual(got, want)

    def test_current_nothing_supported(self):
        self.assertEqual(w.current_tlv(forecast(), Settings()).serialize(), b"")

    def test_current_negative_and_fahrenheit_flag(self):
        f = forecast(temp=-5)
        t = w.current_tlv(f, Settings(current_temperature=True, unit=True), fahrenheit=True)
        self.assertEqual(t.serialize(), b"\x09\x01\xfb\x0a\x01\x01")

    def test_forecast_layout(self):
        f = forecast()
        s = Settings(temperature=True, icon=True, extended_hourly=True, sun_rise_set=True)
        got = w.forecast_tlv(f, s).serialize()
        day0 = NOW // 86400 * 86400
        h1 = b"\x03\x04" + (NOW + 3600).to_bytes(4, "big") + b"\x04\x01\x00\x05\x01\x15\x06\x01\x0a\x07\x01\x02"
        h2 = b"\x03\x04" + (NOW + 7200).to_bytes(4, "big") + b"\x04\x01\x07\x05\x01\xfe\x06\x01\x50\x07\x01\x00"
        hours = b"\x82" + bytes([len(h1)]) + h1 + b"\x82" + bytes([len(h2)]) + h2
        d1 = (b"\x12\x04" + NOW.to_bytes(4, "big") + b"\x13\x01\x01\x14\x01\x20\x15\x01\x13"
              b"\x16\x04" + (day0 + 3600).to_bytes(4, "big") + b"\x17\x04" + (day0 + 40000).to_bytes(4, "big"))
        d2 = b"\x12\x04" + (NOW + 86400).to_bytes(4, "big") + b"\x13\x01\x02\x14\x01\x1e\x15\x01\x12"
        days = b"\x91" + bytes([len(d1)]) + d1 + b"\x91" + bytes([len(d2)]) + d2
        want = b"\x81" + bytes([len(hours)]) + hours + b"\x90" + bytes([len(days)]) + days
        self.assertEqual(got, want)

    def test_forecast_drops_sun_outside_its_day_and_unsupported(self):
        f = forecast()
        f.days[0].sunrise = (NOW // 86400 - 1) * 86400 + 5  # yesterday UTC: fixupWeather zeroes it
        got = w.forecast_tlv(f, Settings(sun_rise_set=True))
        d = got.get_tlv(0x90).get_all(0x91)
        first = TLV.parse(d[0])
        self.assertNotIn(0x16, first)
        self.assertIn(0x17, first)
        bare = w.forecast_tlv(f, Settings())  # nothing supported: only timestamps
        self.assertEqual([t for t, _ in TLV.parse(bare.get_tlv(0x90).get_all(0x91)[0]).items], [0x12])

    def test_icon_mapping(self):
        self.assertEqual([w.icon(c) for c in (0, 2, 3, 45, 53, 61, 63, 65, 66, 71, 73, 75, 80, 95, 99)],
                         [0, 1, 2, 0x12, 7, 7, 8, 9, 0x13, 0x0E, 0x0F, 0x10, 3, 4, 5])
        self.assertEqual(w.icon(1234), 0x63)

    def test_beaufort_and_direction(self):
        self.assertEqual([w.beaufort(k) for k in (0, 3.2, 10, 25, 130)], [0, 1, 2, 4, 12])
        self.assertEqual([w.wind_direction(d) for d in (None, -1, 0, 45, 207, 360, 350)], [0, 0, 8, 1, 5, 8, 8])


OPEN_METEO = {
    "current": {"time": NOW, "temperature_2m": 24.1, "apparent_temperature": 23.6, "relative_humidity_2m": 40,
                "weather_code": 2, "wind_speed_10m": 3.2, "wind_direction_10m": 207},
    "hourly": {"time": [NOW - 7200, NOW - 600, NOW + 3000, NOW + 6600],
               "temperature_2m": [20.0, 21.4, 22.5, None], "weather_code": [0, 1, 61, 3],
               "precipitation_probability": [0, 5, 60, 0], "uv_index": [0.0, 1.2, 0.4, 0.0]},
    "daily": {"time": [NOW - 40000, NOW + 46400], "weather_code": [2, 3], "temperature_2m_max": [31.7, 29.5],
              "temperature_2m_min": [18.8, 18.6], "sunrise": [NOW - 20000, NOW + 66000],
              "sunset": [NOW + 20000, NOW + 106000]},
}


class ParseTest(unittest.TestCase):
    def test_open_meteo_to_model(self):
        f = w.parse_open_meteo(OPEN_METEO, "Cairo")
        self.assertEqual((f.location, f.time, f.code, f.temp, f.feels, f.humidity, f.wind_kmh, f.wind_dir),
                         ("Cairo", NOW, 2, 24, 24, 40, 3.2, 207))
        # hours before now-1h and hours with a null reading are skipped
        self.assertEqual(f.hourly, [Hour(NOW - 600, 1, 21, 5, 1), Hour(NOW + 3000, 61, 22, 60, 0)])
        self.assertEqual(f.days[0], Day(NOW - 40000, 2, 32, 19, NOW - 20000, NOW + 20000))
        self.assertEqual(len(f.days), 2)
        self.assertEqual(f.uv, 1)

    def test_malformed(self):
        with self.assertRaises(KeyError):
            w.parse_open_meteo({}, "x")

    def test_fetcher_urls_and_geocoding_once(self):
        calls = []

        def urlopen(req, timeout):
            calls.append(req.full_url)
            body = ({"results": [{"latitude": 30.06, "longitude": 31.25, "name": "Cairo"}]}
                    if "geocoding" in req.full_url else OPEN_METEO)
            return _Resp(body)

        fetch = w.OpenMeteo(urlopen)
        f = fetch({"city": "Cairo"})
        fetch({"city": "Cairo"})
        self.assertEqual(f.location, "Cairo")
        self.assertEqual(sum("geocoding" in u for u in calls), 1)
        self.assertIn("latitude=30.06", calls[1])
        self.assertIn("timeformat=unixtime", calls[1])

    def test_fixed_location_skips_geocoding(self):
        calls = []
        fetch = w.OpenMeteo(lambda req, timeout: calls.append(req.full_url) or _Resp(OPEN_METEO))
        f = fetch({"latitude": 1, "longitude": 2, "name": "Home"})
        self.assertEqual((f.location, len(calls)), ("Home", 1))

    def test_unknown_city(self):
        fetch = w.OpenMeteo(lambda req, timeout: _Resp({}))
        with self.assertRaises(ValueError):
            fetch({"city": "Nowhere"})


class _Resp(io.BytesIO):
    def __init__(self, body):
        super().__init__(json.dumps(body).encode())

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class ConfigTest(unittest.TestCase):
    def test_config_handling(self):
        self.assertIsNone(WeatherService(None).config)
        self.assertIsNone(WeatherService({}).config)
        self.assertIsNone(WeatherService({"latitude": 1}).config)
        self.assertTrue(WeatherService({"city": "Cairo"}).config)
        self.assertTrue(WeatherService({"latitude": 1, "longitude": 2}).config)
        self.assertFalse(WeatherService({"city": "Cairo"}).fahrenheit)
        self.assertTrue(WeatherService({"city": "Cairo", "units": "imperial"}).fahrenheit)


def run(coro):
    return asyncio.run(coro)


class ServiceTest(unittest.TestCase):
    def svc(self, fetched=None, **kw):
        fetched = fetched if fetched is not None else []
        def fetch(cfg):
            fetched.append(cfg)
            return forecast()
        return WeatherService({"city": "Cairo", **kw}, fetch), fetched

    def test_unconfigured_does_nothing_and_logs_once(self):
        async def go():
            s, session = WeatherService(None, lambda c: 1 / 0), FakeSession()
            await s.setup(session)
            await s.setup(session)
            self.assertEqual((session.requests, session.sent), ([], []))
            self.assertEqual(len(session.logs), 1)
        run(go())

    def test_setup_fetches_then_pushes_in_order(self):
        async def go():
            s, fetched = self.svc()
            session = FakeSession()
            await s.setup(session)
            self.assertEqual(len(fetched), 1)
            self.assertEqual([c for _, c, _ in session.requests],
                             [w.START, w.UNIT, w.SUPPORT, w.EXTENDED, w.SUN_MOON, w.CURRENT, w.FORECAST])
            cur = TLV.parse(dict((c, b) for _, c, b in session.requests)[w.CURRENT])
            self.assertEqual(cur.get_str(8), "Cairo")
            self.assertEqual(cur.get_str(0x0E), "Open-Meteo")
            await s.setup(FakeSession())  # a reconnect reuses the data
            self.assertEqual(len(fetched), 1)
        run(go())

    def test_band_capabilities_gate_the_packets(self):
        async def go():
            s, _ = self.svc()
            session = FakeSession(bitmap=0x01, ext=0, sun=0, caps="")  # weather only
            await s.setup(session)
            cur = dict((c, b) for _, c, b in session.requests)[w.CURRENT]
            self.assertEqual(cur, b"")
        run(go())

    def test_imperial_unit_flag(self):
        async def go():
            s, _ = self.svc(units="imperial")
            session = FakeSession()
            await s.setup(session)
            sent = dict((c, b) for _, c, b in session.requests)
            self.assertEqual(sent[w.UNIT], b"\x01\x01\x01")
            self.assertEqual(TLV.parse(sent[w.CURRENT]).get_int(0x0A), 1)
        run(go())

    def test_failures_logged_not_raised(self):
        async def go():
            def boom(cfg):
                raise OSError("offline")
            s = WeatherService({"city": "Cairo"}, boom)
            session = FakeSession()
            await s.setup(session)  # fetch fails: nothing to push
            self.assertEqual(session.requests, [])
            self.assertTrue(any("fetch failed" in x for x in session.logs))
            s.latest = forecast()
            session.replies[w.START] = TLV().put_u32(0x7F, 0x186A4)  # band refuses
            await s.push(session)
            self.assertEqual(len(session.requests), 1)
            session.replies[w.START] = TLV().put_u32(0x7F, w.OK)
            session.errors[w.SUPPORT] = TimeoutError("no reply")
            await s.push(session)
            session.errors = {w.START: ConnectionError("gone")}
            await s.push(session)
            self.assertTrue(any("push failed" in x for x in session.logs))
            session.disconnected.set()
            session.requests.clear()
            await s.push(session)
            self.assertEqual(session.requests, [])
        run(go())

    def test_missing_optional_commands_still_send_weather(self):
        async def go():
            s, _ = self.svc()
            session = FakeSession()
            session.errors = {w.EXTENDED: TimeoutError("x"), w.SUN_MOON: TimeoutError("x"), w.UNIT: TimeoutError("x")}
            await s.setup(session)
            self.assertEqual([c for _, c, _ in session.requests][-2:], [w.CURRENT, w.FORECAST])
        run(go())

    def test_run_refreshes_and_pushes_to_live_session(self):
        async def go():
            s, fetched = self.svc()
            session = FakeSession()
            await s.setup(session)
            session.requests.clear()
            with mock.patch.object(w, "REFRESH", 0.01):
                task = asyncio.create_task(s.run())
                await asyncio.sleep(0.1)
                task.cancel()
            self.assertGreater(len(fetched), 1)
            self.assertTrue(session.requests)
            session.disconnected.set()
            session.requests.clear()
            with mock.patch.object(w, "REFRESH", 0.01):
                task = asyncio.create_task(s.run())
                await asyncio.sleep(0.05)
                task.cancel()
            self.assertEqual(session.requests, [])  # a dead session gets nothing
        run(go())

    def test_run_survives_fetch_errors(self):
        async def go():
            s = WeatherService({"city": "Cairo"}, lambda c: 1 / 0)
            with mock.patch.object(w, "REFRESH", 0.01):
                task = asyncio.create_task(s.run())
                await asyncio.sleep(0.05)
                self.assertFalse(task.done())
                task.cancel()
        run(go())

    def test_band_request_is_acked_unencrypted(self):
        async def go():
            s, _ = self.svc()
            session = FakeSession()
            await s.setup(session)
            s.on_message(msg(w.DEVICE, TLV().put_u8(1, 2).put_u8(2, 0)))  # what the Band 11 sends after connect
            await asyncio.sleep(0.05)
            self.assertEqual(session.sent, [(0x0F, 4, b"\x01\x04\x00\x01\x86\xa0", False)])
            s.on_message(msg(3, TLV().put_u16(1, 1)))  # 0F/03: unknown, only logged
            s.on_message(msg(4, TLV(), svc=0x25))
            await asyncio.sleep(0.01)
            self.assertEqual(len(session.sent), 1)
        run(go())

    def test_band_asking_for_weather_gets_it(self):
        async def go():
            s, _ = self.svc()
            session = FakeSession()
            await s.setup(session)
            session.requests.clear()
            s.on_message(msg(w.DEVICE, TLV().put_u32(0x7F, w.ASK)))
            await asyncio.sleep(0.05)
            self.assertEqual(session.requests[0][1], w.START)
            self.assertEqual(session.sent, [])  # no ack: Gadgetbridge sends weather instead
        run(go())


if __name__ == "__main__":
    unittest.main()
