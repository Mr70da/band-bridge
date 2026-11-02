# Ported from Gadgetbridge (AGPL-3.0): MusicControl.java, HuaweiMusicManager.java, SetMusicRequest.java,
# SetMusicStatusRequest.java, AsynchronousResponse.java (handleMusicControls)
import asyncio
import ctypes
import logging
import time
from datetime import datetime, timezone
from typing import NamedTuple

from band.packet import TLV

SERVICE = 0x25
OK = 0x186A0  # tag 7F success value
STATUS, INFO, CONTROL = 1, 2, 3  # band asks for info / phone pushes info / band presses a button
ACTIONS = {1: "play", 2: "pause", 3: "previous", 4: "next", 5: "volume_up", 6: "volume_down"}  # CONTROL tag 1
IGNORED = (64, 100)  # tag 1 values Gadgetbridge treats as "no button" (leaving the music screen): not acked
NONE, STOPPED, PAUSED, PLAYING = 0, 1, 2, 3  # Android PlaybackState, which the band expects
MAX_VOLUME, VOLUME = 100, 50  # the band's slider sends 0..MAX_VOLUME; VOLUME when the PC volume can't be read
POLL = 2  # seconds between now-playing checks
DOUBLE_TAP = 1.5  # seconds: a second previous/next within this changes the track
SEEK = 10  # seconds a single previous/next tap jumps back/forward
REFRESH_DELAY = 0.3  # let the player change state before the info that follows a button press (Gadgetbridge: 0.1)
TIMEOUT = 5  # seconds to wait for the band's answer to one push
VK_VOLUME_DOWN, VK_VOLUME_UP = 0xAE, 0xAF

log = logging.getLogger("band.music")


class Track(NamedTuple):
    title: str
    artist: str
    state: int  # NONE / STOPPED / PAUSED / PLAYING
    volume: int = VOLUME  # 0..MAX_VOLUME


def info_tlv(track: Track, max_volume=MAX_VOLUME, volume=None) -> TLV:
    """25/02 phone -> band, MusicControl.MusicInfo.Request."""
    return (TLV().put(1, track.artist).put(2, track.title).put_u8(3, track.state).put_u8(4, max_volume)
            .put_u8(5, track.volume if volume is None else volume))


def status_ack() -> TLV:
    return TLV().put_u32(0x7F, OK)


def parse_control(tlv: TLV) -> tuple[int | None, int | None]:
    """25/03 -> (button, slider volume). Like Gadgetbridge only the lowest byte counts; 64 and 100 are no button.
    The Band 11 sends its volume slider as tag 2, never buttons 5/6. It steps by 6 and runs past 0 as a signed byte
    (04, FE, F8, ...) [log], so the value is read signed and clamped."""
    button = tlv.get_int(1) & 0xFF if 1 in tlv else None
    volume = min(max(int.from_bytes(tlv.get(2), "big", signed=True), 0), MAX_VOLUME) if 2 in tlv else None
    return (None if button in IGNORED else button), volume


# --- the PC side ----------------------------------------------------------------------------------------------------

class _GUID(ctypes.Structure):
    _fields_ = [("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16), ("d3", ctypes.c_uint16), ("d4", ctypes.c_ubyte * 8)]


def _guid(text) -> _GUID:
    g = _GUID()
    ctypes.oledll.ole32.CLSIDFromString(text, ctypes.byref(g))
    return g


def _method(obj, index, *argtypes):
    """COM method `index` of obj's vtable; raises OSError on a failed HRESULT."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return lambda *args: ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtable[index])(obj, *args)


class SystemVolume:
    """The default speakers' master volume (IAudioEndpointVolume), 0..100. Looked up per call: the default device
    changes when headphones are plugged in."""
    CLSID_ENUMERATOR = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
    IID_ENUMERATOR = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
    IID_VOLUME = "{5CDF2C82-841E-4546-9722-0CF74078229A}"

    def _with_endpoint(self, use):
        ctypes.windll.ole32.CoInitializeEx(None, 0)  # S_FALSE / RPC_E_CHANGED_MODE: COM is already up in this thread
        enum, device, volume = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        try:
            ctypes.oledll.ole32.CoCreateInstance(ctypes.byref(_guid(self.CLSID_ENUMERATOR)), None, 1,
                                                 ctypes.byref(_guid(self.IID_ENUMERATOR)), ctypes.byref(enum))
            # IMMDeviceEnumerator::GetDefaultAudioEndpoint(eRender, eMultimedia)
            _method(enum, 4, ctypes.c_int, ctypes.c_int, ctypes.c_void_p)(0, 1, ctypes.byref(device))
            # IMMDevice::Activate(IID_IAudioEndpointVolume, CLSCTX_ALL)
            _method(device, 3, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p)(
                ctypes.byref(_guid(self.IID_VOLUME)), 23, None, ctypes.byref(volume))
            return use(volume)
        finally:
            for obj in (volume, device, enum):
                if obj.value:
                    _method(obj, 2)()  # IUnknown::Release

    def get(self) -> int:
        level = ctypes.c_float()
        self._with_endpoint(lambda v: _method(v, 9, ctypes.c_void_p)(ctypes.byref(level)))  # GetMasterVolumeLevelScalar
        return round(level.value * 100)

    def set(self, percent: int):
        level = min(max(percent, 0), 100) / 100
        self._with_endpoint(lambda v: _method(v, 7, ctypes.c_float, ctypes.c_void_p)(level, None))  # Set...Scalar


class WindowsMedia:
    """Windows.Media.Control for now-playing and transport, media keys for the volume."""
    # GlobalSystemMediaTransportControlsSessionPlaybackStatus: closed, opened, changing, stopped, playing, paused
    STATES = {3: STOPPED, 4: PLAYING, 5: PAUSED}

    def __init__(self, volume=None):
        self._manager = None
        self.system_volume = volume or SystemVolume()

    async def _current(self):
        if self._manager is None:
            from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager
            self._manager = await Manager.request_async()
        return self._manager.get_current_session()

    def volume(self) -> int:
        try:
            return self.system_volume.get()
        except OSError:  # no audio device
            return VOLUME

    async def now_playing(self) -> Track:
        session = await self._current()
        if session is None:
            return Track("", "", NONE, self.volume())
        props = await session.try_get_media_properties_async()
        state = self.STATES.get(int(session.get_playback_info().playback_status), NONE)
        return Track(props.title, props.artist, state, self.volume())

    async def _call(self, name, allowed=None, *args) -> bool:
        """False when there is no player or it does not allow the action (YouTube outside a playlist has no
        next/previous). The players' own answer is no use: Chrome says True and ignores it [live test]."""
        session = await self._current()
        if session is None or (allowed and not getattr(session.get_playback_info().controls, allowed)):
            return False
        return await getattr(session, name)(*args)

    async def play(self): return await self._call("try_play_async")
    async def pause(self): return await self._call("try_pause_async")
    async def next(self): return await self._call("try_skip_next_async", "is_next_enabled")
    async def previous(self): return await self._call("try_skip_previous_async", "is_previous_enabled")
    async def seek(self, seconds) -> bool:
        """Jump seconds back (negative) or forward from where the player is now."""
        session = await self._current()
        if session is None or not session.get_playback_info().controls.is_playback_position_enabled:
            return False
        t = session.get_timeline_properties()
        position = t.position.total_seconds()
        if int(session.get_playback_info().playback_status) == 4:  # playing: the reported position is from then
            position += (datetime.now(timezone.utc) - t.last_updated_time).total_seconds()
        target = min(max(position + seconds, 0), t.end_time.total_seconds())
        return await session.try_change_playback_position_async(int(target * 10_000_000))  # 100 ns ticks

    async def volume_up(self): self._key(VK_VOLUME_UP)
    async def volume_down(self): self._key(VK_VOLUME_DOWN)

    async def set_volume(self, percent): self.system_volume.set(percent)

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
        self._last_tap = (None, 0.0)  # (previous/next, monotonic time) of the last single tap

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
                if button is None and volume is None:
                    return  # nothing pressed: no ack, no refresh
                if volume is not None:
                    await self.media.set_volume(volume)
                action = ACTIONS.get(button)
                args = ()
                if action in ("previous", "next"):  # one tap: seek SEEK s; a second tap: change the track
                    now, (last, at) = time.monotonic(), self._last_tap
                    if last == action and now - at < DOUBLE_TAP:
                        self._last_tap = (None, 0.0)
                    else:
                        self._last_tap = (action, now)
                        action, args = "seek", (-SEEK if action == "previous" else SEEK,)
                if action and await getattr(self.media, action)(*args) is False:
                    self._log(f"the player can't {action}", session)
                await session.send(SERVICE, CONTROL, status_ack())
                if button is None:
                    return  # a slider drag sends ~5 values a second; the watcher reports the volume afterwards
                await asyncio.sleep(REFRESH_DELAY)
            await self.push(session)
        except Exception as e:
            self._log(f"25/{msg.command:02X} handling failed: {e!r}", session)
