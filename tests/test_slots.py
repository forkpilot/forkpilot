"""Machine-wide SITL instance claiming (no SITL started)."""
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from forkpilot import battery


class SlotTest(unittest.TestCase):
    def test_claims_are_exclusive_and_skip_busy_ports(self):
        with mock.patch.object(battery, "SLOTS", Path(tempfile.mkdtemp())):
            a, fa = battery.claim_instance(0)
            b, fb = battery.claim_instance(0)
            self.assertNotEqual(a, b)
            fa.close()
            c, fc = battery.claim_instance(0)
            self.assertEqual(c, a)                    # released slot is reused
            with socket.socket() as s:                # a SITL started outside ForkPilot
                s.bind(("127.0.0.1", 5760 + 10 * 7))
                d, fd = battery.claim_instance(7)
                self.assertNotEqual(d, 7)
            for f in (fb, fc, fd):
                f.close()


if __name__ == "__main__":
    unittest.main()
