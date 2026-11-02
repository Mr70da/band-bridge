import asyncio
import types
import unittest
from unittest import mock

from bridge import win_notify
from bridge.win_notify import NotificationMirror


def toast(i, app="Slack", app_id="Slack.App", title="Hi", lines=("one", "two")):
    return (i, app, app_id, title, list(lines))


class Feed:
    def __init__(self, *polls):
        self.polls = list(polls)

    async def __call__(self):
        p = self.polls.pop(0)
        if isinstance(p, Exception):
            raise p
        return p


class MirrorTest(unittest.IsolatedAsyncioTestCase):
    def make(self, *polls, config=None, away=True, fail=False):
        sent = []

        async def send(title, text):
            if fail:
                raise ConnectionError("band not connected")
            sent.append((title, text))

        p = mock.patch.object(win_notify.idle, "away", return_value=away)
        p.start()
        self.addCleanup(p.stop)
        return NotificationMirror(send, config, read=Feed(*polls)), sent

    async def polls(self, m, n):
        for _ in range(n):
            await m.poll()

    async def test_no_backlog_then_new_toast_formatted(self):
        m, sent = self.make([toast(1)], [toast(1), toast(2, "Mail", "Mail.App", "Subject", ["a", "b"])])
        await self.polls(m, 2)
        self.assertEqual(sent, [("Mail: Subject", "a b")])

    async def test_dedupe_and_dismissed_toasts(self):
        m, sent = self.make([], [toast(2)], [toast(2)], [], [toast(3)])
        await self.polls(m, 5)
        self.assertEqual(len(sent), 2)

    async def test_title_only_toast(self):
        m, sent = self.make([], [toast(1, lines=())])
        await self.polls(m, 2)
        self.assertEqual(sent, [("Slack: Hi", "")])

    async def test_ignore_defaults_apps_filter_and_override(self):
        new = [toast(1, "Claude", "Claude_x!Claude"), toast(2, "Python", "py.exe"), toast(3, "Slack"), toast(4, "Mail", "Mail.App")]
        for cfg, want in [(None, ["Slack", "Mail"]), ({"apps": ["MAIL.app"]}, ["Mail"]),
                          ({"ignore": []}, ["Claude", "Python", "Slack", "Mail"])]:
            m, sent = self.make([], new, config=cfg)
            await self.polls(m, 2)
            self.assertEqual([t.split(":")[0] for t, _ in sent], want)

    async def test_only_when_away(self):
        m, sent = self.make([], [toast(1)], [toast(1), toast(2)], away=False)
        await self.polls(m, 3)
        self.assertEqual(sent, [])  # present: dropped, and not replayed on a later poll
        m, sent = self.make([], [toast(1)], config={"only_when_away": False}, away=False)
        await self.polls(m, 2)
        self.assertEqual(len(sent), 1)

    async def test_send_failure_is_dropped_not_retried(self):
        m, sent = self.make([], [toast(1)], [toast(1)], fail=True)
        await self.polls(m, 3)
        self.assertEqual(sent, [])

    async def test_run_survives_poll_errors(self):
        m, sent = self.make([toast(1)], RuntimeError("boom"), [toast(1), toast(2)])
        with mock.patch.object(win_notify, "POLL", 0):
            task = asyncio.create_task(m.run())
            while m.read.polls:
                await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
        self.assertEqual(len(sent), 1)

    async def test_disabled_run_returns(self):
        m, _ = self.make(config={"enabled": False})
        await asyncio.wait_for(m.run(), 1)


class ReadToastsTest(unittest.IsolatedAsyncioTestCase):
    async def test_read_toasts_maps_winrt_objects(self):
        text = lambda s: types.SimpleNamespace(text=s)
        info = types.SimpleNamespace(display_info=types.SimpleNamespace(display_name="Slack"), app_user_model_id="S.App")
        full = types.SimpleNamespace(id=7, app_info=info, notification=types.SimpleNamespace(visual=types.SimpleNamespace(
            get_binding=lambda name: types.SimpleNamespace(get_text_elements=lambda: [text("T"), text("B1"), text("B2")]))))
        empty = types.SimpleNamespace(id=8, app_info=info, notification=types.SimpleNamespace(
            visual=types.SimpleNamespace(get_binding=lambda name: None)))

        async def get(kinds):
            return [full, empty]

        listener = types.SimpleNamespace(current=types.SimpleNamespace(get_notifications_async=get))
        with mock.patch("winrt.windows.ui.notifications.management.UserNotificationListener", listener):
            self.assertEqual(await win_notify.read_toasts(), [(7, "Slack", "S.App", "T", ["B1", "B2"])])


if __name__ == "__main__":
    unittest.main()
