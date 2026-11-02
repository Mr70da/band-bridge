# Ported from Gadgetbridge (AGPL-3.0): MusicControl.java, HuaweiMusicManager.java, SetMusicRequest.java,
# SetMusicStatusRequest.java, AsynchronousResponse.java (handleMusicControls)
import asyncio
import ctypes
import logging
from typing import NamedTuple

from band.packet import TLV

SERVICE = 0x25
OK = 0x186A0  # tag 7F success value
STATUS, INFO, CONTROL = 1, 2, 3  # band asks for info / phone pushes info / band presses a button
ACTIONS = {1: "play", 2: "pause", 3: "previous", 4: "next", 5: "volume_up", 6: "volume_down"}  # CONTROL tag 1
IGNORED = (64, 100)  # tag 1 values Gadgetbridge treats as "no button" (leaving the music screen): not acked
NONE, STOPPED, PAUSED, PLAYING = 0, 1, 2, 3  # Android PlaybackState, which the band expects
MAX_VOLUME, VOLUME = 100, 50  # shortcut: constants, the PC master volume is not read; read it via IAudioEndpointVolume
POLL = 2  # seconds between now-playing checks
REFRESH_DELAY = 0.3  # let the player change state before the info that follows a button press (Gadgetbridge: 0.1)
TIMEOUT = 5  # seconds to wait for the band's answer to one push
VK_VOLUME_DOWN, VK_VOLUME_UP = 0xAE, 0xAF

log = logging.getLogger("band.music")


class Track(NamedTuple):
    title: str
    artist: str
    state: int  # NONE / STOPPED / PAUSED / PLAYING


def info_tlv(track: Track, max_volume=MAX_VOLUME, volume=VOLUME) -> TLV:
    """25/02 phone -> band, MusicControl.MusicInfo.Request."""
    return (TLV().put(1, track.artist).put(2, track.title).put_u8(3, track.state).put_u8(4, max_volume)
            .put_u8(5, volume))


def status_ack() -> TLV:
    return TLV().put_u32(0x7F, OK)


def parse_control(tlv: TLV) -> tuple[int | None, bool]:
    """25/03 -> (button, volume present). Like Gadgetbridge only the lowest byte counts; 64 and 100 are no button."""
    button = tlv.get_int(1) & 0xFF if 1 in tlv else None
    return (None if button in IGNORED else button), 2 in tlv


# --- the PC side ----------------------------------------------------------------------------------------------------

class WindowsMedia:
    """Windows.Media.Control for now-playing and transport, media keys for the volume."""
    # GlobalSystemMediaTransportControlsSessionPlaybackStatus: closed, opened, changing, stopped, playing, paused
    STATES = {3: STOPPED, 4: PLAYING, 5: PAUSED}

    def __init__(self):
        self._manager = None

    async def _current(self):
        if self._manager is None:
            from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager
            self._manager = await Manager.request_async()
        return self._manager.get_current_session()

    async def now_playing(self) -> Track:
        session = await self._current()
        if session is None:
            return Track("", "", NONE)
        props = await session.try_get_media_properties_async()
        return Track(props.title, props.artist, self.STATES.get(int(session.get_playback_info().playback_status), NONE))

    async def _call(self, name):
        if (session := await self._current()) is not None:
            await getattr(session, name)()

    async def play(self): await self._call("try_play_async")
    async def pause(self): await self._call("try_pause_async")
    async def next(self): await self._call("try_skip_next_async")
    async def previous(self): await self._call("try_skip_previous_async")

    async def volume_up(self): self._key(VK_VOLUME_UP)
    async def volume_down(self): self._key(VK_VOLUME_DOWN)

    @staticmethod
    def _key(vk):
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk, 0, 2, 0)  # KEYEVENTF_KEYUP


# --- the service ----------------------------------------------------------------------------------------------------

class MusicService:
    """Survives reconnects: setup(session) on every new Session; run() is the one long-lived watcher."""

    def __init__(self, media=None):
        self.media = media or WindowsMedia()
        self._session = None
        self._pushed = None  # last Track offered to the band
        self._lock = asyncio.Lock()  # one 25/02 request at a time (Session.request allows one per command)
        self._tasks = set()

    def _log(self, text, session=None):
        session = session or self._session
        if session is not None:
            session.transport.log("EVT", f"music {text}")
        else:
            log.warning(text)

    def _live(self):
        s = self._session
        return s is not None and not s.disconnected.is_set()

    async def setup(self, session):
        """The band needs no setup (Gadgetbridge only reads upload parameters, for music files). Just push the track."""
        self._session = session
        await self.push(session)

    async def push(self, session=None, track=None):
        """Send the current track (25/02). Failures are logged, never raised."""
        session = session or self._session
        if session is None or session.disconnected.is_set():
            return
        try:
            track = track or await self.media.now_playing()
            async with self._lock:
                self._pushed = track
                t = (await session.request(SERVICE, INFO, info_tlv(track), timeout=TIMEOUT)).tlv
            if t.get_int(0x7F, OK) != OK:
                self._log(f"25/02 rejected, 7F={t.get_int(0x7F):X}", session)
        except Exception as e:
            self._log(f"push failed: {e!r}", session)

    async def run(self):
        """Poll the PC's now-playing; push when it changed and a live session is set."""
        while True:
            try:
                track = await self.media.now_playing()
                if track != self._pushed and self._live():
                    await self.push(self._session, track)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log(f"now-playing failed: {e!r}")
            await asyncio.sleep(POLL)

    def on_message(self, msg):
        if msg.service != SERVICE or msg.tlv is None or msg.command not in (STATUS, CONTROL) or self._session is None:
            return
        task = asyncio.get_running_loop().create_task(self._handle(self._session, msg))
        self._tasks.add(task)  # the loop only keeps weak references
        task.add_done_callback(self._tasks.discard)

    async def _handle(self, session, msg):
        try:
            if msg.command == STATUS:
                status = msg.tlv.get_int(0x7F, -1)
                if status not in (-1, OK):
                    self._log(f"25/01 error {status:X}, ignored", session)
                    return
                await session.send(SERVICE, STATUS, status_ack())
            else:
                button, volume = parse_control(msg.tlv)
                if button in ACTIONS:
                    await getattr(self.media, ACTIONS[button])()
                if button is None and not volume:
                    return  # nothing pressed: no ack, no refresh
                # shortcut: a band-side volume value (tag 2) is acked but not applied, the PC volume is not settable
                await session.send(SERVICE, CONTROL, status_ack())
                await asyncio.sleep(REFRESH_DELAY)
            await self.push(session)
        except Exception as e:
            self._log(f"25/{msg.command:02X} handling failed: {e!r}", session)
