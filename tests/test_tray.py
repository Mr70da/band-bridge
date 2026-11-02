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
        self.assertEqual(tray.tooltip("ready", 77), "Band Bridge: ready (battery 77%)")
        self.assertEqual(tray.tooltip("backoff"), "Band Bridge: backoff")

    def test_dot_is_an_image(self):
        self.assertEqual(tray.dot(tray.GREEN).getpixel((32, 32))[:3], tray.GREEN)


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
