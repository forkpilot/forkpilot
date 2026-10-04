"""SITL link handling without a real SITL: a TCP peer that accepts and closes."""
import socket
import threading
import time
import unittest

from pymavlink import mavutil

from forkpilot import sitl as sl
from forkpilot.runner import Runner, Run, ScenarioError


def closing_server():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()

    def serve():
        conn, _ = srv.accept()
        conn.close()
    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1]


class LinkTest(unittest.TestCase):
    def test_closed_link_fails_fast(self):
        port = closing_server()
        mav = mavutil.mavlink_connection(f"tcp:127.0.0.1:{port}", autoreconnect=False)
        mav.handle_eof = sl._closed
        t = time.time()
        with self.assertRaises(ConnectionError):
            mav.wait_heartbeat(timeout=5)
        self.assertLess(time.time() - t, 1)

    def test_closed_link_mid_flight_is_scenario_error(self):
        port = closing_server()
        s = sl.Sitl()
        s.mav = mavutil.mavlink_connection(f"tcp:127.0.0.1:{port}", autoreconnect=False)
        r = Runner(s, Run(scenario="x"))
        with self.assertRaises(ScenarioError):
            r.wait(lambda: False, 60, "nothing")


if __name__ == "__main__":
    unittest.main()
