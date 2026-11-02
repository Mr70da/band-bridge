import io
import json
import unittest
from unittest import mock

import tray
from bridge import api


class TrayTest(unittest.TestCase):
    def test_colors(self):
        for state in ("ready",):
            self.assertEqual(tray.color_for(state), tray.GREEN)
        for state in ("connecting", "backoff"):
            self.assertEqual(tray.color_for(state), tray.AMBER)
        for state in ("needs_pairing", "stopped", "error", "starting"):
            self.assertEqual(tray.color_for(state), tray.RED)

    def test_tooltip(self):
        self.assertEqual(tray.tooltip("ready", 77), "Band Bridge: ready, battery 77%")
        self.assertEqual(tray.tooltip("backoff"), "Band Bridge: backoff")

    def test_battery_alert_hysteresis(self):
        warned, toasts = False, []
        for level in (50, 16, 15, 14, 10, 17, 14, 20, 14, 21, 14, None):
            warned, toast = tray.battery_alert(level, warned)
            toasts.append(toast)
        # toasts at the first 14 and, after rising above 20, at the next 14 (the last-but-one value)
        self.assertEqual(toasts, [False, False, False, True, False, False, False, False, False, False, True, False])

    def test_dot_is_an_image(self):
        self.assertEqual(tray.dot(tray.GREEN).getpixel((32, 32))[:3], tray.GREEN)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def respond(body):
    return FakeResponse(json.dumps(body).encode())


class TrayActionsTest(unittest.TestCase):
    def setUp(self):
        with mock.patch("pystray.Icon"):  # a real one registers a window class, which a second one in the process can't
            self.tray = tray.Tray()

    def test_poll_updates_battery_state_and_tooltip(self):
        with mock.patch.object(tray.urllib.request, "urlopen", return_value=respond({"state": "ready", "battery": 61})) as u:
            self.tray.poll_once()
        self.assertTrue(u.call_args.args[0].endswith("/status"))
        self.assertEqual((self.tray.state, self.tray.battery), ("ready", 61))
        self.assertEqual(self.tray.icon.title, "Band Bridge: ready, battery 61%")

    def test_poll_toasts_once_when_the_battery_gets_low(self):
        for level in (30, 14, 13):
            with mock.patch.object(tray.urllib.request, "urlopen", return_value=respond({"state": "ready", "battery": level})):
                self.tray.poll_once()
        self.tray.icon.notify.assert_called_once()
        self.assertIn("14%", self.tray.icon.notify.call_args.args[0])

    def test_poll_keeps_the_last_values_when_the_api_is_down(self):
        self.tray.battery = 50
        with mock.patch.object(tray.urllib.request, "urlopen", side_effect=OSError("refused")):
            self.tray.poll_once()
        self.assertEqual(self.tray.battery, 50)
        self.tray.icon.notify.assert_not_called()

    def test_poll_loop_stops_on_quit(self):
        self.tray.stopped.set()
        with mock.patch.object(self.tray, "poll_once") as once:
            self.tray.poll()  # returns at once instead of looping forever
        once.assert_not_called()


class ChainTest(unittest.TestCase):
    def test_extra_callback_gets_every_state(self):
        seen = []
        with self.assertLogs("bridge", "INFO") as logs:
            api.chain_on_state(lambda s, d: seen.append((s, d)))("ready", "battery 77%")
        self.assertEqual(seen, [("ready", "battery 77%")])
        self.assertIn("band ready battery 77%", logs.output[0])

    def test_without_extra(self):
        with self.assertLogs("bridge", "INFO"):
            api.chain_on_state()("connecting", "")

    def test_create_app_hands_it_to_the_real_supervisor(self):
        seen = []
        with mock.patch.object(api, "Supervisor") as sup, mock.patch.object(api, "Notifier"):
            api.create_app(on_state=lambda s, d: seen.append(s))
        with self.assertLogs("bridge", "INFO"):
            sup.call_args.kwargs["on_state"]("backoff", "x")
        self.assertEqual(seen, ["backoff"])


if __name__ == "__main__":
    unittest.main()
