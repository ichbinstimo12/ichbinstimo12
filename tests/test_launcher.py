import asyncio
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from src.firm.api import launcher


class H(BaseHTTPRequestHandler):
    ready = True
    def log_message(self, *a): pass
    def do_GET(self):
        self.send_response(200 if H.ready else 503); self.end_headers(); self.wfile.write(b"{}")


class LauncherTests(unittest.TestCase):
    def test_url_with_and_without_token(self):
        self.assertEqual(launcher.dashboard_url("127.0.0.1", 8000, ""), "http://127.0.0.1:8000/")
        self.assertEqual(launcher.dashboard_url("0.0.0.0", 8000, "HIER_EINFUEGEN"), "http://127.0.0.1:8000/")
        self.assertEqual(launcher.dashboard_url("127.0.0.1", 8000, "a b+c"), "http://127.0.0.1:8000/?token=a%20b%2Bc")

    def test_opens_browser_when_server_ready(self):
        H.ready = True
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        with mock.patch("webbrowser.open", return_value=True) as wb:
            ok = asyncio.run(launcher.open_when_ready("127.0.0.1", port, "tok123", timeout=5, interval=0.05))
        srv.shutdown()
        self.assertTrue(ok)
        wb.assert_called_once_with(f"http://127.0.0.1:{port}/?token=tok123")

    def test_waits_then_opens_and_gives_up_cleanly(self):
        H.ready = False
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        def flip(): 
            import time; time.sleep(0.4); H.ready = True
        threading.Thread(target=flip, daemon=True).start()
        with mock.patch("webbrowser.open", return_value=True) as wb:
            self.assertTrue(asyncio.run(launcher.open_when_ready("127.0.0.1", port, "", timeout=5, interval=0.05)))
        srv.shutdown()
        with mock.patch("webbrowser.open") as wb2:   # nichts laeuft -> nach Timeout False, kein Absturz
            self.assertFalse(asyncio.run(launcher.open_when_ready("127.0.0.1", 9, "", timeout=0.3, interval=0.05)))
            wb2.assert_not_called()

    def test_browser_error_never_crashes(self):
        H.ready = True
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        with mock.patch("webbrowser.open", side_effect=RuntimeError("kein Browser")):
            self.assertFalse(asyncio.run(launcher.open_when_ready("127.0.0.1", srv.server_address[1], "", timeout=3, interval=0.05)))
        srv.shutdown()


if __name__ == "__main__":
    unittest.main()
