#!/usr/bin/env python3
"""Exercise mount-admin routing after URL source authentication.

Usage: python3 tests/admin_auth/run.py --icecast /path/to/rebuilt/icecast
Requires ffmpeg; runs only temporary servers on loopback ports.
"""
import argparse
import base64
import http.client
import http.server
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run(icecast):
    records = []
    lock = threading.Lock()

    class Backend(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            data = dict(urllib.parse.parse_qsl(body.decode(), keep_blank_values=True))
            with lock:
                records.append(data)
            self.send_response(200)
            if data.get("user") == "source" and data.get("pass") == "source-pass":
                self.send_header("icecast-auth-user", "1")
            self.send_header("Content-Length", "0")
            self.end_headers()

    backend = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Backend)
    backend_thread = threading.Thread(target=backend.serve_forever, daemon=True)
    backend_thread.start()
    stopped = threading.Event()
    sources = []
    feeders = []

    with tempfile.TemporaryDirectory(prefix="icecast-admin-auth-") as tmp:
        root = Path(tmp)
        for name in ("web", "admin", "log"):
            (root / name).mkdir()
        (root / "admin/listclients.xsl").write_text('''<xsl:stylesheet version="1.0"
xmlns:xsl="http://www.w3.org/1999/XSL/Transform"><xsl:output method="text"/>
<xsl:template match="/"><xsl:text>LISTCLIENTS </xsl:text>
<xsl:value-of select="icestats/source/@mount"/></xsl:template></xsl:stylesheet>''')
        port = free_port()
        mounts = "".join(f'''<mount><mount-name>{mount}</mount-name>
<authentication type="url"><option name="stream_auth"
value="http://127.0.0.1:{backend.server_port}/auth"/>
<option name="timeout" value="3"/></authentication></mount>'''
                         for mount in ("/live", "/fresh", "/admin/metadata", "/denied"))
        (root / "icecast.xml").write_text(f'''<icecast>
<limits><clients>50</clients><sources>6</sources><workers>4</workers></limits>
<authentication><source-password>source-pass</source-password>
<admin-user>admin</admin-user><admin-password>admin-pass</admin-password></authentication>
<hostname>127.0.0.1</hostname><listen-socket><bind-address>127.0.0.1</bind-address>
<port>{port}</port></listen-socket><fileserve>1</fileserve>
<paths><logdir>{root}/log</logdir><webroot>{root}/web</webroot>
<adminroot>{root}/admin</adminroot><alias source="/listeners" dest="/admin/listclients"/></paths>
<logging><errorlog>error.log</errorlog><accesslog>access.log</accesslog><loglevel>4</loglevel></logging>
{mounts}<mount><mount-name>/static</mount-name><password>source-pass</password></mount>
</icecast>''')
        audio = subprocess.check_output([
            "ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
            "sine=frequency=440:sample_rate=44100", "-t", "0.3", "-c:a", "libmp3lame",
            "-b:a", "64k", "-f", "mp3", "pipe:1",
        ])

        def request(path, credentials="source:source-pass", method="GET", limit=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                auth = base64.b64encode(credentials.encode()).decode()
                conn.request(method, path, headers={
                    "Authorization": "Basic " + auth, "Connection": "close",
                })
                response = conn.getresponse()
                return response.status, response.read(limit)
            finally:
                conn.close()

        def start_source(mount, method="SOURCE", credentials="source:source-pass", extra=""):
            sock = socket.create_connection(("127.0.0.1", port), timeout=5)
            sources.append(sock)
            auth = base64.b64encode(credentials.encode()).decode()
            sock.sendall((f"{method} {mount} HTTP/1.0\r\nHost: localhost\r\n"
                          f"Authorization: Basic {auth}\r\nContent-Type: audio/mpeg\r\n"
                          f"{extra}\r\n").encode() + audio)
            response = b""
            while b"\r\n\r\n" not in response:
                part = sock.recv(4096)
                assert part, response
                response += part
            status = int(response.split(b" ", 2)[1])
            if status == 200:
                def feed():
                    while not stopped.wait(0.1):
                        try:
                            sock.sendall(audio)
                        except OSError:
                            return
                thread = threading.Thread(target=feed, daemon=True)
                thread.start()
                feeders.append(thread)
            return status

        def expect(path, status=200, contains=None, **kwargs):
            code, body = request(path, **kwargs)
            assert code == status, (path, code, body[:500])
            if contains is not None:
                assert contains in body, (path, body[:500])
            return body

        with (root / "stdout.log").open("wb") as output:
            server = subprocess.Popen([icecast, "-c", str(root / "icecast.xml")],
                                      stdout=output, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 10
                while True:
                    assert server.poll() is None, "Icecast exited during startup"
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                            break
                    except OSError:
                        assert time.monotonic() < deadline, "Icecast did not listen"
                        time.sleep(0.05)

                assert start_source("/live") == 200
                body = expect("/admin/listclients?mount=/live", contains=b"<icestats>")
                assert ET.fromstring(body).find("source").get("mount") == "/live"
                expect("/admin/listclients.xsl?mount=/live", contains=b"LISTCLIENTS /live")
                expect("/listeners?mount=/live", contains=b"<icestats>")
                expect("/admin/listclients?mount=/live", method="HEAD")
                expect("/admin/stats?mount=/live", contains=b"<icestats>")
                expect("/admin/updatemetadata?mount=/live", contains=b"<icestats>")
                expect("/admin/metadata?mount=/live&song=Regression", contains=b"Metadata update successful")
                expect("/admin.cgi?mount=/live&pass=source-pass&mode=updinfo&song=Legacy",
                       status=400, contains=b"Not a shoutcast compatible stream")
                assert len(expect("/admin/streams?mount=/live", limit=1024)) == 1024

                # Rejected credentials must reach neither admin handlers nor source startup.
                expect("/admin/listclients?mount=/live", status=401,
                       credentials="source:wrong")
                expect("/listeners?mount=/live", status=401, credentials="source:wrong")
                assert start_source("/denied", credentials="source:wrong") == 401

                # Successful admin auth on an unused mount must not create a source there.
                expect("/admin/metadata?mount=/fresh&song=Absent", status=400)
                # Genuine sources retain their route, even with admin-looking paths/headers.
                assert start_source("/fresh", method="PUT", extra="__admin_cmd: listclients\r\n") == 200
                assert start_source("/admin/metadata") == 200
                assert start_source("/static") == 200
                expect("/admin/listclients?mount=/static", contains=b"<icestats>")
                expect("/admin/listclients?mount=/static", status=401, credentials="source:wrong")
                with lock:
                    count = len(records)
                expect("/admin/listclients?mount=/live", credentials="admin:admin-pass",
                       contains=b"<icestats>")
                with lock:
                    assert len(records) == count, "global admin unexpectedly used URL auth"
                    assert any(r.get("admin") == "1" and r.get("mount") == "/live"
                               for r in records), records
                expect("/live", contains=audio[100:150], limit=4096)
                assert server.poll() is None
                print("PASS: URL-auth admin commands, XSL/HEAD/aliases, denied credentials, "
                      "source startup, static/global auth, continued audio")
            except BaseException:
                output.flush()
                for path in (root / "stdout.log", root / "log/error.log"):
                    if path.exists():
                        print(path.read_text(errors="replace")[-6000:])
                raise
            finally:
                stopped.set()
                for sock in sources:
                    sock.close()
                for thread in feeders:
                    thread.join(timeout=2)
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
                backend.shutdown()
                backend.server_close()
            assert server.returncode == 0, (root / "stdout.log").read_text(errors="replace")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--icecast", required=True)
    run(str(Path(parser.parse_args().icecast).resolve()))
