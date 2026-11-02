"""Mirror new Windows toasts to the band. Reads them via UserNotificationListener, which works for unpackaged apps here."""
import asyncio
import logging

from bridge import idle

log = logging.getLogger("bridge")
POLL = 3.0
IGNORE = ["python", "pythonw", "pystray", "claude"]  # our own toasts, and Claude Code's, which are already on the band


async def read_toasts() -> list[tuple]:
    """Current Action Center toasts as (id, app name, app id, title, body lines)."""
    from winrt.windows.ui.notifications import NotificationKinds
    from winrt.windows.ui.notifications.management import UserNotificationListener
    out = []
    for n in await UserNotificationListener.current.get_notifications_async(NotificationKinds.TOAST):
        binding = n.notification.visual.get_binding("ToastGeneric")
        texts = [t.text for t in binding.get_text_elements()] if binding else []
        if not texts:
            continue
        info = n.app_info
        out.append((n.id, info.display_info.display_name, info.app_user_model_id, texts[0], texts[1:]))
    return out


class NotificationMirror:
    def __init__(self, send, config=None, read=read_toasts):
        self.send, self.read = send, read
        cfg = config or {}
        self.enabled = cfg.get("enabled", True)
        self.only_away = cfg.get("only_when_away", True)
        self.apps = [a.lower() for a in cfg.get("apps") or []]
        self.ignore = [a.lower() for a in cfg.get("ignore", IGNORE)]
        self._seen: set | None = None  # None until the first poll: whatever is already there is backlog

    def wanted(self, name: str, app_id: str) -> bool:
        hay = f"{name}\n{app_id}".lower()
        return not any(i in hay for i in self.ignore) and (not self.apps or any(a in hay for a in self.apps))

    async def poll(self):
        toasts = await self.read()
        seen, self._seen = self._seen, {t[0] for t in toasts}
        if seen is None or (self.only_away and not idle.away()):
            return  # backlog, or the user is at the PC; either way these count as seen
        for _id, name, app_id, title, lines in toasts:
            if _id in seen or not self.wanted(name, app_id):
                continue
            try:
                await self.send(f"{name}: {title}", " ".join(lines))
            except Exception as e:  # band not connected: drop, don't retry
                log.debug("mirror send dropped: %r", e)

    async def run(self):
        while self.enabled:
            try:
                await self.poll()
            except Exception as e:
                log.warning("notification mirror poll failed: %r", e)
            await asyncio.sleep(POLL)
