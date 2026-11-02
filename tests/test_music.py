import asyncio
import unittest
from unittest import mock

from band.packet import TLV, Message
from band.services import music as m
from band.services.music import MusicService, Track

SONG = Track("Song", "Artist", m.PLAYING)
ACK = b"\x7f\x04\x00\x01\x86\xa0"


def msg(cmd, tlv, svc=0x25) -> Message:
    return Message(svc, cmd, tlv.serialize(), tlv)


class FakeMedia:
    def __init__(self, track=SONG):
        self.track, self.calls, self.fail = track, [], False

    async def now_playing(self):
        if self.fail:
            raise OSError("no media")
        return self.track

    def __getattr__(self, name):
        async def act():
            self.calls.append(name)
        return act


class FakeSession:
    def __init__(self):
        self.requests, self.sent, self.logs = [], [], []
        self.disconnected = asyncio.Event()
        self.reply = TLV().put_u32(0x7F, m.OK)
        self.error = None
        self.transport = mock.Mock(log=lambda way, text: self.logs.append(text))

    async def request(self, svc, cmd, tlv, timeout=10):
        if self.error:
            raise self.error
        self.requests.append((svc, cmd, tlv.serialize()))
        return msg(cmd, self.reply)

    async def send(self, svc, cmd, tlv):
        self.sent.append((svc, cmd, tlv.serialize()))


class PacketTest(unittest.TestCase):
    def test_music_info_layout(self):
        # MusicControl.MusicInfo.Request: 01 artist, 02 song, 03 state, 04 max volume, 05 current volume
        self.assertEqual(m.info_tlv(Track("Song", "Artist", m.PLAYING), 15, 7).serialize(),
                         b"\x01\x06Artist" b"\x02\x04Song" b"\x03\x01\x03" b"\x04\x01\x0f" b"\x05\x01\x07")

    def test_empty_track(self):
        self.assertEqual(m.info_tlv(Track("", "", m.NONE), 100, 50).serialize(),
                         b"\x01\x00" b"\x02\x00" b"\x03\x01\x00" b"\x04\x01\x64" b"\x05\x01\x32")

    def test_non_ascii_is_utf8(self):
        self.assertEqual(m.info_tlv(Track("é", "", m.PAUSED)).get(2), "é".encode())

    def test_status_ack(self):
        # MusicStatusRequest: 7F = 0x000186A0 as 4 bytes
        self.assertEqual(m.status_ack().serialize(), ACK)

    def test_parse_control(self):
        self.assertEqual(m.parse_control(TLV().put_u8(1, 4)), (4, False))
        self.assertEqual(m.parse_control(TLV().put_u32(1, 0x104)), (4, False))  # only the lowest byte counts
        self.assertEqual(m.parse_control(TLV().put_u8(2, 9)), (None, True))
        self.assertEqual(m.parse_control(TLV().put_u8(1, 5).put_u8(2, 9)), (5, True))
        for ignored in (64, 100):  # leaving the music screen
            self.assertEqual(m.parse_control(TLV().put_u8(1, ignored)), (None, False))

    def test_button_codes(self):
        self.assertEqual(m.ACTIONS, {1: "play", 2: "pause", 3: "previous", 4: "next",
                                     5: "volume_up", 6: "volume_down"})


class ServiceTest(unittest.IsolatedAsyncioTestCase):
    async def drain(self, svc):
        while svc._tasks:
            await asyncio.gather(*svc._tasks)

    def make(self, media=None):
        s, svc = FakeSession(), MusicService(media or FakeMedia())
        svc._session = s
        return s, svc

    async def test_setup_pushes_current_track(self):
        s, svc = FakeSession(), MusicService(FakeMedia())
        await svc.setup(s)
        self.assertEqual(s.requests, [(0x25, 2, m.info_tlv(SONG).serialize())])
        self.assertEqual(s.logs, [])

    async def test_band_status_request_is_acked_then_info_follows(self):
        s, svc = self.make()
        svc.on_message(msg(1, TLV().put_u32(0x7F, m.OK)))
        await self.drain(svc)
        self.assertEqual(s.sent, [(0x25, 1, ACK)])
        self.assertEqual(len(s.requests), 1)

    async def test_band_status_request_without_tags_is_acked(self):
        s, svc = self.make()
        svc.on_message(msg(1, TLV()))
        await self.drain(svc)
        self.assertEqual((len(s.sent), len(s.requests)), (1, 1))

    async def test_band_status_error_stops(self):
        s, svc = self.make()
        svc.on_message(msg(1, TLV().put_u32(0x7F, 0x1234)))
        await self.drain(svc)
        self.assertEqual((s.sent, s.requests), ([], []))
        self.assertIn("1234", s.logs[0])

    async def test_buttons_map_to_media_actions(self):
        for button, action in m.ACTIONS.items():
            media = FakeMedia()
            s, svc = self.make(media)
            with mock.patch.object(m, "REFRESH_DELAY", 0):
                svc.on_message(msg(3, TLV().put_u8(1, button)))
                await self.drain(svc)
            self.assertEqual(media.calls, [action])
            self.assertEqual(s.sent, [(0x25, 3, ACK)])
            self.assertEqual(len(s.requests), 1)  # fresh info after the press

    async def test_ignored_and_empty_control_get_no_ack(self):
        for tlv in (TLV().put_u8(1, 64), TLV().put_u8(1, 100), TLV()):
            media = FakeMedia()
            s, svc = self.make(media)
            svc.on_message(msg(3, tlv))
            await self.drain(svc)
            self.assertEqual((media.calls, s.sent, s.requests), ([], [], []))

    async def test_unknown_button_and_volume_value_are_acked_without_action(self):
        for tlv in (TLV().put_u8(1, 9), TLV().put_u8(2, 8)):
            media = FakeMedia()
            s, svc = self.make(media)
            with mock.patch.object(m, "REFRESH_DELAY", 0):
                svc.on_message(msg(3, tlv))
                await self.drain(svc)
            self.assertEqual(media.calls, [])
            self.assertEqual(len(s.sent), 1)

    async def test_other_messages_and_no_session_are_ignored(self):
        svc = MusicService(FakeMedia())
        svc.on_message(msg(3, TLV().put_u8(1, 1)))  # no session yet
        svc._session = FakeSession()
        svc.on_message(msg(3, TLV().put_u8(1, 1), svc=0x02))
        svc.on_message(msg(2, TLV().put_u32(0x7F, m.OK)))
        self.assertEqual(svc._tasks, set())

    async def test_failed_action_is_logged_not_raised(self):
        media = FakeMedia()
        media.play = mock.AsyncMock(side_effect=OSError("boom"))
        s, svc = self.make(media)
        svc.on_message(msg(3, TLV().put_u8(1, 1)))
        await self.drain(svc)
        self.assertIn("boom", s.logs[0])

    async def test_push_failures_are_logged_never_raised(self):
        s, svc = FakeSession(), MusicService(FakeMedia())
        s.error = TimeoutError("no reply to 25/02")
        await svc.setup(s)
        self.assertIn("push failed", s.logs[0])
        s.error, s.reply = None, TLV().put_u32(0x7F, 0x5555)
        await svc.push(s)
        self.assertIn("rejected", s.logs[1])
        s.disconnected.set()
        s.requests.clear()
        await svc.push(s)  # dead session: silently skipped
        self.assertEqual(s.requests, [])
        await MusicService(FakeMedia()).push()  # no session at all

    async def test_watcher_pushes_only_on_change(self):
        media = FakeMedia()
        s, svc = self.make(media)
        with mock.patch.object(m, "POLL", 0.01):
            task = asyncio.create_task(svc.run())
            await asyncio.sleep(0.1)
            self.assertEqual(len(s.requests), 1)  # many polls, one push
            media.track = Track("Other", "Artist", m.PAUSED)
            await asyncio.sleep(0.1)
            self.assertEqual(len(s.requests), 2)
            self.assertEqual(s.requests[1][2], m.info_tlv(media.track).serialize())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_watcher_skips_dead_session_and_setup_catches_up(self):
        svc = MusicService(FakeMedia())
        with mock.patch.object(m, "POLL", 0.01):
            task = asyncio.create_task(svc.run())
            await asyncio.sleep(0.05)  # no session
            dead = svc._session = FakeSession()
            dead.disconnected.set()
            await asyncio.sleep(0.05)
            self.assertEqual(dead.requests, [])
            live = FakeSession()
            await svc.setup(live)  # the next connection gets the track at once
            self.assertEqual(len(live.requests), 1)
            task.cancel()

    async def test_watcher_survives_media_errors(self):
        media = FakeMedia()
        s, svc = self.make(media)
        media.fail = True
        with mock.patch.object(m, "POLL", 0.01):
            task = asyncio.create_task(svc.run())
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())
            media.fail = False
            await asyncio.sleep(0.05)
            self.assertEqual(len(s.requests), 1)
            task.cancel()
        self.assertTrue(any("now-playing failed" in x for x in s.logs))


class WindowsMediaTest(unittest.TestCase):
    def test_state_mapping(self):
        # closed, opened, changing, stopped, playing, paused -> Android PlaybackState
        self.assertEqual([m.WindowsMedia.STATES.get(i, m.NONE) for i in range(6)],
                         [m.NONE, m.NONE, m.NONE, m.STOPPED, m.PLAYING, m.PAUSED])

    def test_volume_keys(self):
        with mock.patch.object(m.ctypes, "windll") as dll:
            m.WindowsMedia._key(m.VK_VOLUME_UP)
        self.assertEqual([c.args for c in dll.user32.keybd_event.call_args_list],
                         [(0xAF, 0, 0, 0), (0xAF, 0, 2, 0)])


if __name__ == "__main__":
    unittest.main()
