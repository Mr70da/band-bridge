import os
import tempfile
import unittest
from pathlib import Path

from band.transport import FrameAssembler, Transport, chunks

LINK_PARAMS = bytes.fromhex("5A000B0001010100020003000400F13B")
REPLY_PARTS = [  # real Band 11 link-params reply, as three notifications
    bytes.fromhex("5A0035000101010102020203FE03020014040200"),
    bytes.fromhex("0005120001F4458C8644C8F429E7EF6CFF8BC44E"),
    bytes.fromhex("A30701040901000D01000F01011001001A41"),
]


class ChunksTest(unittest.TestCase):
    def test_49_bytes_by_20(self):
        frame = bytes(range(49))
        parts = chunks(frame, 20)
        self.assertEqual([len(p) for p in parts], [20, 20, 9])
        self.assertEqual(b"".join(parts), frame)


class LogPathTest(unittest.TestCase):
    def test_given_log_path_is_appended_to_by_every_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "logs", "run.log")
            for n in (1, 2):
                t = Transport("AA:BB", log_path=path)
                t.log("EVT", f"session {n}")
                t._log.close()
            self.assertEqual(t.log_path, path)
            self.assertEqual([line.split(" ", 2)[2] for line in Path(path).read_text().splitlines()],
                             ["EVT session 1", "EVT session 2"])

    def test_default_log_path_is_new_per_transport_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Transport("AA:BB", log_dir=tmp)
            t._log.close()
            self.assertRegex(os.path.basename(t.log_path), r"^ble-\d{8}-\d{6}\.log$")
            self.assertEqual(os.path.dirname(t.log_path), tmp)


class FrameAssemblerTest(unittest.TestCase):
    def test_reply_split_in_three(self):
        asm = FrameAssembler()
        self.assertEqual(asm.feed(REPLY_PARTS[0]), [])
        self.assertEqual(asm.feed(REPLY_PARTS[1]), [])
        self.assertEqual(asm.feed(REPLY_PARTS[2]), [b"".join(REPLY_PARTS)])

    def test_two_frames_in_one_feed(self):
        self.assertEqual(FrameAssembler().feed(LINK_PARAMS * 2), [LINK_PARAMS, LINK_PARAMS])

    def test_garbage_before_frame_is_dropped(self):
        asm = FrameAssembler()
        self.assertEqual(asm.feed(b"\x00\x11" + LINK_PARAMS), [LINK_PARAMS])
        self.assertEqual(asm.dropped, [b"\x00\x11"])


if __name__ == "__main__":
    unittest.main()
