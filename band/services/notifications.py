# Ported from Gadgetbridge (AGPL-3.0): Notifications.java, SendNotificationRequest.java,
# HuaweiNotificationsManager.java, P2P.java, HuaweiP2PCannedRepliesService.java, HuaweiBaseP2PService.java
import asyncio
import random
import time

from band.packet import TLV

QUICK_REPLIES = ["1", "2", "3", "yes", "no", "continue", "stop"]  # config key "quick_replies" overrides

GENERIC = 0x7F  # notificationType: chat apps map to generic
ENCODING = 2  # Gadgetbridge forces content format 02 even when the band reports 03
DEFAULT_LIMIT = 500  # Band 11 [log]; Gadgetbridge's own default is a cautious 15
KEEP = 50  # sent notifications remembered for reply lookup
TIMEOUT = 5  # seconds to wait for the band's answer to one message

# protocol.md 8.4
MODULE, PEER = "hw.unitedevice.smsquick", "com.huawei.watch.home"
LOCAL_FP, PEER_FP = "UniteDeviceManagement", "603AC6A57E2023E00C9C93BB539CA653DF3003EBA4E92EA1904BA4AAA5D938F0"
QUERY, UPDATE = 7001, 7002


# --- notifications (service 0x02) -----------------------------------------------------------------------------------

def notification_tlv(msg_id, ntype, title, sender, body, source_app, category="", reply_key=None, ts=None, key=None,
                     nid=0) -> TLV:
    """NotificationActionRequest. An optional part is left out when its argument is None (empty for category)."""
    texts = TLV()
    for kind, value in ((3, title), (2, sender), (1, body)):
        item = TLV().put_u8(0x0E, kind).put_u8(0x0F, ENCODING)
        if value:
            item.put(0x10, value)
        texts.put(0x8D, item)
    tlv = (TLV().put_u16(1, msg_id).put_u8(2, ntype).put(3, True).put(0x84, TLV().put(0x8C, texts))
           .put(0x11, source_app[:127]))
    if category:
        tlv.put(0x12, category)
    if reply_key is not None:
        tlv.put(0x18, reply_key)
    if ts is not None:
        tlv.put_u32(0x15, ts)
    if key is not None:
        tlv.put(0x19, key).put_u32(0x1A, nid).put(0x1B, "")
    return tlv


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 13] + "..."


def content_limit(tlv: TLV) -> int:
    """02/02 reply -> max body length (the entry of text type 1), DEFAULT_LIMIT if the reply has another shape."""
    try:
        for entry in tlv.get_tlv(0x81).get_tlv(0x82).get_tlv(0x90).get_all(0x91):
            entry = TLV.parse(entry)
            if entry.get_int(0x12) == 1:
                return entry.get_int(0x14) or DEFAULT_LIMIT
    except (KeyError, ValueError):
        pass
    return DEFAULT_LIMIT


def decode_reply(raw: bytes, encoding: int) -> str:
    if encoding != 3:
        return raw.decode("utf-8", "replace")
    # Java's UTF_16 reads BOM-less data as big-endian, Python's as little-endian
    return raw.decode("utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-16-be", "replace")


def reply_ack(tlv: TLV) -> TLV | None:
    """Only an SMS reply (type 1) is acked, with 0xFF = OK; a notification reply (type 2) gets none."""
    return TLV().put_u8(7, 0xFF) if tlv.get_int(1, 0) == 1 else None


# --- quick replies over P2P (service 0x34) --------------------------------------------------------------------------

def p2p_packet(seq: int, data: TLV) -> TLV:
    return (TLV().put_u8(1, 2).put_u16(2, seq).put(3, MODULE).put(4, PEER).put(5, LOCAL_FP).put(6, PEER_FP)
            .put(7, data))


def replies_update(replies) -> TLV:
    items = TLV()
    for reply in replies:
        items.put(0x83, TLV().put(4, reply))
    return TLV().put_u32(1, UPDATE).put(0x82, items)


def parse_replies(data: TLV) -> list[str]:
    if 0x82 not in data:
        return []
    slots = (TLV.parse(r).get(4, b"").decode("utf-8", "replace") for r in data.get_tlv(0x82).get_all(0x83))
    return [text for text in slots if text]  # the band may pad the list with empty slots


def band_data(msg) -> TLV | None:
    """The inner TLV of a band cmdId 2 packet addressed to the quick-reply module, else None."""
    t = msg.tlv
    if (msg.service, msg.command) != (0x34, 1) or t is None or t.get_int(1, 0) != 2 or 7 not in t:
        return None
    if t.get(4, b"") != MODULE.encode():
        return None
    try:
        return TLV.parse(t.get(7))
    except ValueError:
        return None


async def _p2p(session, tlv: TLV, match, what):
    """Send a 34/01 and wait for the band's answer. The waiter is registered first so a fast answer is not missed."""
    waiter = asyncio.ensure_future(session.wait_for(match, TIMEOUT))
    try:
        await session.send(0x34, 1, tlv)
    except BaseException:
        waiter.cancel()
        raise
    try:
        return await waiter
    except TimeoutError:
        raise TimeoutError(f"no {what} from the band") from None


async def set_quick_replies(session, replies) -> int:
    """Replaces the band's list; returns the code of the band's ack (0xCF)."""
    if not replies:
        raise ValueError("the quick-reply list must not be empty")  # protocol.md 8.4
    seq = session.next_p2p_seq()

    def acked(msg):
        t = msg.tlv
        return (msg.service, msg.command) == (0x34, 1) and t is not None and t.get_int(1, 0) == 3 \
            and t.get_int(2, -1) == seq

    ack = await _p2p(session, p2p_packet(seq, replies_update(replies)), acked, "ack of the quick-reply update")
    return ack.tlv.get_int(8, 0)


async def get_quick_replies(session) -> list[str]:
    def answer(msg):
        data = band_data(msg)
        return data is not None and data.get_int(1, 0) == QUERY

    query = p2p_packet(session.next_p2p_seq(), TLV().put_u32(1, QUERY))
    return parse_replies(band_data(await _p2p(session, query, answer, "quick-reply list")))


# --- the long-lived part --------------------------------------------------------------------------------------------

async def _ask(session, cmd, tlv) -> TLV:
    """Request on service 2. No reply or an error code is logged, never raised: the band may not answer every one."""
    try:
        t = (await session.request(2, cmd, tlv, timeout=TIMEOUT)).tlv
    except TimeoutError as e:
        session.transport.log("EVT", f"notify {e}")
        return TLV()
    if t.get_int(0x7F, 0x186A0) != 0x186A0:
        session.transport.log("EVT", f"notify 02/{cmd:02X} rejected, 7F={t.get_int(0x7F):X}")
    return t


class Notifier:
    """Survives reconnects (the Supervisor makes a new Session each time): call setup(session) on every new one."""

    def __init__(self, on_reply=None):
        self.on_reply = on_reply  # (notification id or None, text, key)
        self.sent: dict[str, int] = {}  # reply key -> our notification id, oldest first
        self.capabilities = None
        self.content_limit = DEFAULT_LIMIT
        self.quick_replies: list[str] = []
        self._session = None
        self._next_id = random.randint(1, 10000)  # random start: ids must not repeat after a restart
        self._msg_id = 0
        self._tasks = set()

    async def setup(self, session):
        self._session = session
        t = await _ask(session, 5, TLV().put(1))
        self.capabilities = t.get_int(1) if 1 in t else None
        self.content_limit = content_limit(await _ask(session, 2, TLV().put(1)))
        await _ask(session, 4, TLV().put(0x81, TLV().put(2, True).put(3, True)))
        await _ask(session, 8, TLV().put(1, True))
        try:
            code = await self.set_replies(session)
            self.quick_replies = await get_quick_replies(session)
            quick = f"update acked {code:X}, band has {self.quick_replies}"
        except (TimeoutError, ValueError) as e:  # not worth dropping the link for
            quick = f"quick replies failed: {e}"
        session.transport.log("EVT", f"notify setup: capabilities {self.capabilities}, "
                                     f"content limit {self.content_limit}, {quick}")

    async def set_replies(self, session, replies=None) -> int:
        """Replaces the band's quick replies; None = the default list (config "quick_replies" or QUICK_REPLIES)."""
        if replies is None:
            replies = session.config.get("quick_replies", QUICK_REPLIES)
        return await set_quick_replies(session, replies)

    async def send(self, session, title, text, reply=True, source_app="org.telegram.messenger", category="msg",
                   sender="Claude Code") -> int:
        self._next_id += 1
        self._msg_id = (self._msg_id + 1) & 0xFFFF
        nid = self._next_id
        key = f"0|{source_app}|{nid}||0"
        self.sent[key] = nid
        if len(self.sent) > KEEP:
            del self.sent[next(iter(self.sent))]
        # Gadgetbridge sends 18 on every notification when the band has cap 89: empty means "no reply button"
        tlv = notification_tlv(self._msg_id, GENERIC, title, sender, clip(text, self.content_limit), source_app,
                               category, key if reply else "", int(time.time()), key, nid)
        await _ask(session, 1, tlv)
        return nid

    async def remove(self, session, notification_id, category="msg") -> bool:
        """False if we never sent that id (the band is addressed by its key)."""
        key = next((k for k, v in self.sent.items() if v == notification_id), None)
        if key is None:
            return False
        tlv = (TLV().put_u8(1, GENERIC).put(2, key.split("|")[1]).put(3, key).put_u32(4, notification_id).put(5, ""))
        if category:
            tlv.put(6, category)
        await _ask(session, 6, tlv)
        return True

    def handle(self, msg) -> tuple[int | None, str] | None:
        """A 02/10 reply event -> (our notification id or None if unknown, text); anything else -> None."""
        t = msg.tlv
        if (msg.service, msg.command) != (2, 0x10) or t is None or 6 not in t:
            return None
        key = t.get(4, b"").decode("utf-8", "replace")
        text = decode_reply(t.get(6), t.get_int(2, 0))
        nid = self.sent.get(key)
        if self.on_reply:
            self.on_reply(nid, text, key)
        return nid, text

    def on_message(self, msg):
        """For Session/Supervisor on_message."""
        if self.handle(msg) is not None and self._session and (ack := reply_ack(msg.tlv)):
            task = asyncio.get_running_loop().create_task(self._send_ack(self._session, ack))
            self._tasks.add(task)  # the loop only keeps weak references
            task.add_done_callback(self._tasks.discard)

    async def _send_ack(self, session, ack):
        try:
            await session.send(2, 0x10, ack)
        except Exception as e:
            session.transport.log("EVT", f"notify reply ack failed {e!r}")
