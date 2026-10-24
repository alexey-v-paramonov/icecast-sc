#!/usr/bin/env python3
import argparse
import base64
import http.server
import os
import pathlib
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse


records = []
record_lock = threading.Lock()
drop_armed = {"value": True}


class Backend(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        data = dict(urllib.parse.parse_qsl(body, keep_blank_values=True))
        with record_lock:
            records.append(data)
        if (
            data.get("action") == "listener_remove"
            and data.get("mount", "").startswith("/disabled")
            and drop_armed["value"]
        ):
            drop_armed["value"] = False
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        self.send_response(200)
        self.send_header("icecast-auth-user", "1")
        self.end_headers()
        try:
            self.wfile.write(b"ok")
        except (BrokenPipeError, ConnectionResetError):
            pass


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_port(port, process):
    deadline = time.time() + 10
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError("icecast exited with %s" % process.returncode)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("icecast did not listen")


def open_listener(port, path, agent):
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    credentials = base64.b64encode(b"review:pass").decode()
    request = (
        "GET %s HTTP/1.0\r\nHost: localhost\r\nAuthorization: Basic %s\r\n"
        "User-Agent: %s\r\n\r\n" % (path, credentials, agent)
    )
    sock.sendall(request.encode())
    data = b""
    while b"\r\n\r\n" not in data:
        part = sock.recv(4096)
        if not part:
            break
        data += part
    if b"200" not in data.split(b"\r\n", 1)[0]:
        sock.close()
        raise RuntimeError("listener rejected: %r" % data[:80])
    return sock


def close_listener(sock, reset):
    if reset:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()


def wait_for(predicate, timeout, description):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise RuntimeError("timed out waiting for " + description)


def mount_records(mount, action):
    with record_lock:
        return [
            item
            for item in records
            if item.get("mount", "").startswith(mount) and item.get("action") == action
        ]


def run_handoff(source_bin, icecast_bin):
    backend = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Backend)
    backend_thread = threading.Thread(target=backend.serve_forever, daemon=True)
    backend_thread.start()
    base = pathlib.Path(tempfile.mkdtemp(prefix="icecast-auth-handoff-"))
    for name in ("log", "web", "admin"):
        (base / name).mkdir()
    port = free_port()
    backend_url = "http://127.0.0.1:%d" % backend.server_port
    conf = base / "icecast.xml"
    conf.write_text(
        """<icecast>
<limits><clients>50</clients><sources>4</sources><workers>4</workers><queue-size>524288</queue-size><client-timeout>10</client-timeout><header-timeout>5</header-timeout><source-timeout>10</source-timeout></limits>
<authentication><source-password>reviewpw</source-password><relay-password>reviewpw</relay-password><admin-user>admin</admin-user><admin-password>reviewpw</admin-password></authentication>
<hostname>127.0.0.1</hostname><listen-socket><port>%d</port><bind-address>127.0.0.1</bind-address></listen-socket>
<fileserve>1</fileserve><paths><logdir>%s/log</logdir><webroot>%s/web</webroot><adminroot>%s/admin</adminroot></paths>
<logging><accesslog>access.log</accesslog><errorlog>error.log</errorlog><loglevel>3</loglevel></logging>
<mount><mount-name>/ok</mount-name><authentication type="url"><option name="handlers" value="4"/><option name="allow_duplicate_users" value="1"/><option name="timeout" value="2"/><option name="listener_add" value="%s/add"/><option name="listener_remove_sc" value="%s/remove"/></authentication></mount>
<mount><mount-name>/disabled</mount-name><authentication type="url"><option name="handlers" value="4"/><option name="allow_duplicate_users" value="1"/><option name="timeout" value="2"/><option name="listener_add" value="%s/add"/><option name="listener_remove_sc" value="%s/drop"/></authentication></mount>
</icecast>"""
        % (port, base, base, base, backend_url, backend_url, backend_url, backend_url)
    )
    stdout = (base / "stdout.log").open("wb")
    env = dict(
        os.environ,
        ASAN_OPTIONS="detect_leaks=1:halt_on_error=1:abort_on_error=1:log_path=%s/asan"
        % base,
    )
    process = subprocess.Popen(
        [icecast_bin, "-c", str(conf)], stdout=stdout, stderr=subprocess.STDOUT, env=env
    )
    sources = []
    try:
        wait_port(port, process)
        for mount in ("ok", "disabled"):
            sources.append(
                subprocess.Popen(
                    [
                        source_bin,
                        "-nostdin",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-re",
                        "-f",
                        "lavfi",
                        "-i",
                        "sine=frequency=440:sample_rate=44100",
                        "-codec:a",
                        "libmp3lame",
                        "-b:a",
                        "64k",
                        "-content_type",
                        "audio/mpeg",
                        "-f",
                        "mp3",
                        "icecast://source:reviewpw@127.0.0.1:%d/%s" % (port, mount),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=(base / ("source-%s.log" % mount)).open("wb"),
                )
            )
        time.sleep(1)
        ok_sockets = [
            open_listener(port, "/ok?handoff=a%26b", "HandoffOK/%d" % index)
            for index in range(4)
        ]
        for index, sock in enumerate(ok_sockets):
            close_listener(sock, index % 2 == 0)
        wait_for(lambda: len(mount_records("/ok", "listener_remove")) == 4, 20, "ok removes")
        ok_removes = mount_records("/ok", "listener_remove")
        required = {
            "action",
            "server",
            "port",
            "client",
            "mount",
            "user",
            "pass",
            "ip",
            "agent",
            "duration",
        }
        exact = all(set(item) == required for item in ok_removes)
        identity = all(
            item["client"] != "0"
            and item["ip"] == "127.0.0.1"
            and item["user"] == "review"
            and item["pass"] == "pass"
            and item["agent"].startswith("HandoffOK/")
            for item in ok_removes
        )
        disabled_first = open_listener(port, "/disabled?handoff=1", "HandoffDisabled/1")
        close_listener(disabled_first, False)
        wait_for(
            lambda: len(mount_records("/disabled", "listener_remove")) == 1,
            20,
            "disabled remove attempt",
        )
        time.sleep(0.5)
        disabled_second = open_listener(port, "/disabled?handoff=2", "HandoffDisabled/2")
        close_listener(disabled_second, True)
        time.sleep(3)
        disabled_removes = mount_records("/disabled", "listener_remove")
        immediate = len(disabled_removes) == 1
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=30)
        stdout.close()
        text = (base / "stdout.log").read_text(errors="replace")
        sanitizer_files = list(base.glob("asan*"))
        sanitizer = any(
            marker in text
            for marker in ("AddressSanitizer", "LeakSanitizer", "runtime error:")
        ) or any(
            any(
                marker in path.read_text(errors="replace")
                for marker in ("AddressSanitizer", "LeakSanitizer", "runtime error:")
            )
            for path in sanitizer_files
        )
        print("ok_removes=%d exact_fields=%s identity=%s" % (len(ok_removes), exact, identity))
        print("disabled_remove_posts=%d immediate_disabled=%s" % (len(disabled_removes), immediate))
        print("shutdown_exit=%d sanitizer=%s" % (process.returncode, sanitizer))
        if process.returncode != 0 or not exact or not identity or not immediate or sanitizer:
            return 1
        return 0
    finally:
        for source in sources:
            source.terminate()
            try:
                source.wait(timeout=5)
            except subprocess.TimeoutExpired:
                source.kill()
                source.wait()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        backend.shutdown()
        backend.server_close()
        stdout.close()


def run_shutdown(icecast_bin, allow, source=False):
    arrived = threading.Event()
    release = threading.Event()

    class HeldBackend(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            arrived.set()
            release.wait(25)
            self.send_response(200)
            if allow:
                self.send_header("icecast-auth-user", "1")
            self.end_headers()
            try:
                self.wfile.write(b"ok")
            except (BrokenPipeError, ConnectionResetError):
                pass

    backend = http.server.ThreadingHTTPServer(("127.0.0.1", 0), HeldBackend)
    backend_thread = threading.Thread(target=backend.serve_forever, daemon=True)
    backend_thread.start()
    base = pathlib.Path(tempfile.mkdtemp(prefix="icecast-auth-shutdown-"))
    for name in ("log", "web", "admin"):
        (base / name).mkdir()
    port = free_port()
    conf = base / "icecast.xml"
    conf.write_text(
        """<icecast>
<limits><clients>50</clients><sources>4</sources><workers>2</workers><queue-size>524288</queue-size><client-timeout>10</client-timeout><header-timeout>5</header-timeout><source-timeout>10</source-timeout></limits>
<authentication><source-password>reviewpw</source-password><relay-password>reviewpw</relay-password><admin-user>admin</admin-user><admin-password>reviewpw</admin-password></authentication>
<hostname>127.0.0.1</hostname><listen-socket><port>%d</port><bind-address>127.0.0.1</bind-address></listen-socket>
<fileserve>1</fileserve><paths><logdir>%s/log</logdir><webroot>%s/web</webroot><adminroot>%s/admin</adminroot></paths>
<logging><accesslog>access.log</accesslog><errorlog>error.log</errorlog><loglevel>4</loglevel></logging>
<mount><mount-name>/shutdown</mount-name><authentication type="url"><option name="handlers" value="1"/><option name="timeout" value="30"/><option name="%s" value="http://127.0.0.1:%d/add"/></authentication></mount>
</icecast>"""
        % (port, base, base, base, "stream_auth" if source else "listener_add", backend.server_port)
    )
    stdout = (base / "stdout.log").open("wb")
    env = dict(
        os.environ,
        ASAN_OPTIONS="detect_leaks=1:halt_on_error=1:abort_on_error=1:log_path=%s/asan"
        % base,
        UBSAN_OPTIONS="halt_on_error=1:print_stacktrace=1",
    )
    process = subprocess.Popen(
        [icecast_bin, "-c", str(conf)], stdout=stdout, stderr=subprocess.STDOUT, env=env
    )
    listener = None
    stopped = False
    try:
        wait_port(port, process)
        listener = socket.create_connection(("127.0.0.1", port), timeout=5)
        if source:
            creds = base64.b64encode(b"source:reviewpw").decode()
            listener.sendall(
                ("SOURCE /shutdown ICE/1.0\r\nHost: localhost\r\n"
                 "Authorization: Basic %s\r\nUser-Agent: SourceShutdownReview\r\n"
                 "Content-Type: audio/mpeg\r\n\r\n" % creds).encode()
            )
        else:
            listener.sendall(
                b"GET /shutdown HTTP/1.0\r\nHost: localhost\r\n"
                b"User-Agent: ShutdownReview\r\n\r\n"
            )
        if not arrived.wait(10):
            raise RuntimeError("auth backend was not called")
        process.send_signal(signal.SIGTERM)
        error_log = base / "log" / "error.log"
        deadline = time.time() + 15
        while time.time() < deadline:
            if error_log.exists() and "stopping incoming worker thread" in error_log.read_text(
                errors="replace"
            ):
                stopped = True
                break
            time.sleep(0.05)
        if not stopped:
            raise RuntimeError("incoming worker shutdown was not reached")
        time.sleep(0.25)
        release.set()
        process.wait(timeout=30)
        stdout.close()
        text = (base / "stdout.log").read_text(errors="replace")
        sanitizer_files = list(base.glob("asan*"))
        sanitizer = any(
            marker in text
            for marker in ("AddressSanitizer", "LeakSanitizer", "runtime error:")
        ) or any(
            any(
                marker in path.read_text(errors="replace")
                for marker in ("AddressSanitizer", "LeakSanitizer", "runtime error:")
            )
            for path in sanitizer_files
        )
        mode = ("source_" if source else "") + ("allow" if allow else "deny")
        print(
            "shutdown_%s_exit=%d incoming_stop=%s sanitizer=%s fixture=%s"
            % (mode, process.returncode, stopped, sanitizer, base)
        )
        if process.returncode != 0 or not stopped or sanitizer:
            return 1
        return 0
    finally:
        release.set()
        if listener is not None:
            listener.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        backend.shutdown()
        backend.server_close()
        stdout.close()


def run(source_bin, icecast_bin):
    if run_handoff(source_bin, icecast_bin) != 0:
        return 1
    for source in (False, True):
        for allow in (False, True):
            if run_shutdown(icecast_bin, allow, source) != 0:
                return 1
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--icecast", required=True)
    parser.add_argument("--source", default="ffmpeg")
    args = parser.parse_args()
    return run(args.source, args.icecast)


if __name__ == "__main__":
    sys.exit(main())
