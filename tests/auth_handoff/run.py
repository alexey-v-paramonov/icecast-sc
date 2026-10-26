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
initial_bodies = {}


class Backend(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        data = dict(urllib.parse.parse_qsl(body, keep_blank_values=True))
        if self.path.startswith("/delayed_add"):
            time.sleep(2.5)
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


def open_listener(port, path, agent, headers=None, version="1.0", rcvbuf=None):
    sock = socket.socket()
    if rcvbuf:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    sock.settimeout(5)
    sock.connect(("127.0.0.1", port))
    credentials = base64.b64encode(b"review:pass").decode()
    extra = "".join("%s: %s\r\n" % item for item in (headers or {}).items())
    request = (
        "GET %s HTTP/%s\r\nHost: localhost\r\nAuthorization: Basic %s\r\n"
        "User-Agent: %s\r\n%s\r\n" % (path, version, credentials, agent, extra)
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
    initial_bodies[id(sock)] = data.split(b"\r\n\r\n", 1)[1]
    return sock


def read_body(sock, expected, timeout):
    total = len(initial_bodies.get(id(sock), b""))
    sock.settimeout(0.2)
    deadline = time.time() + timeout
    while total < expected and time.time() < deadline:
        try:
            part = sock.recv(8192)
        except socket.timeout:
            continue
        except OSError:
            break
        if not part:
            break
        total += len(part)
    return total


def close_listener(sock, reset):
    initial_bodies.pop(id(sock), None)
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
            "intro_token",
            "intro_bytes",
            "intro_result",
            "intro_end_ms",
            "intro_elapsed_ms",
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




def _make_intro(path, seconds, rate="64k", sample_rate=44100):
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=%d" % sample_rate,
            "-t", str(seconds), "-codec:a", "libmp3lame", "-b:a", rate,
            str(path),
        ],
        check=True,
    )


def _id3_length(data):
    if data[:3] != b"ID3" or len(data) < 10:
        return 0
    return 10 + (((data[6] & 0x7f) << 21) | ((data[7] & 0x7f) << 14) |
                 ((data[8] & 0x7f) << 7) | (data[9] & 0x7f))


def _strip_id3(path):
    data = path.read_bytes()
    path.write_bytes(data[_id3_length(data):])


def run_intros(source_bin, icecast_bin):
    base = pathlib.Path(tempfile.mkdtemp(prefix="icecast-intro-"))
    for name in ("log", "web", "admin"):
        (base / name).mkdir()
    intros = base / "web" / "intros"
    intros.mkdir()
    _make_intro(intros / "intro_swap_g1.mp3", 4)
    _make_intro(intros / "intro_swap_g2.mp3", 4)
    _make_intro(intros / "intro_burst.mp3", 5, "16k", 22050)
    _make_intro(intros / "intro_id3.mp3", 5, "16k", 22050)
    _make_intro(intros / "intro_huge.mp3", 30)
    _make_intro(intros / "intro_early.mp3", 4)
    _make_intro(intros / "intro_short.mp3", 1)
    _make_intro(intros / "intro_tail.mp3", 1)
    id3_tag = _id3_length((intros / "intro_id3.mp3").read_bytes())
    for name in ("intro_swap_g1", "intro_swap_g2", "intro_burst",
                 "intro_huge", "intro_early", "intro_short", "intro_tail"):
        _strip_id3(intros / (name + ".mp3"))
    early_data = (intros / "intro_early.mp3").read_bytes()
    (intros / "intro_early.mp3").write_bytes(early_data[:-100])
    (intros / "intro_swap.mp3").symlink_to("intro_swap_g1.mp3")
    tail_data = (intros / "intro_tail.mp3").read_bytes()
    (intros / "intro_tail.mp3").write_bytes(tail_data + b"\0")
    burst_size = (intros / "intro_burst.mp3").stat().st_size
    id3_size = (intros / "intro_id3.mp3").stat().st_size
    huge_size = (intros / "intro_huge.mp3").stat().st_size
    early_size = (intros / "intro_early.mp3").stat().st_size
    tail_size = (intros / "intro_tail.mp3").stat().st_size

    backend = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Backend)
    backend_thread = threading.Thread(target=backend.serve_forever, daemon=True)
    backend_thread.start()
    port = free_port()
    backend_url = "http://127.0.0.1:%d" % backend.server_port
    auth = (
        '<authentication type="url"><option name="handlers" value="4"/>'
        '<option name="allow_duplicate_users" value="1"/>'
        '<option name="timeout" value="5"/>'
        '<option name="listener_add" value="%s/add"/>'
        '<option name="listener_remove_sc" value="%s/remove"/>'
        '</authentication>' % (backend_url, backend_url)
    )
    delayed_auth = auth.replace("%s/add" % backend_url, "%s/delayed_add" % backend_url)
    mounts = "".join(
        '<mount><mount-name>/%s</mount-name><intro>intros/%s</intro>%s%s</mount>'
        % (name, intro, this_auth, extra)
        for name, intro, this_auth, extra in (
            ("swap", "intro_swap.mp3", auth, ""),
            ("burst", "intro_burst.mp3", auth, ""),
            ("id3", "intro_id3.mp3", auth, ""),
            ("early", "intro_early.mp3", auth, ""),
            ("chunk", "intro_huge.mp3", auth,
             "<max-listener-duration>2</max-listener-duration>"),
            ("tail", "intro_tail.mp3", auth, ""),
            ("fail", "intro_huge.mp3", auth, ""),
            ("missing", "intro_missing.mp3", auth, ""),
            ("delayed", "intro_short.mp3", delayed_auth, ""),
        )
    )
    conf = base / "icecast.xml"
    conf.write_text(
        """<icecast>
<limits><clients>100</clients><sources>12</sources><workers>4</workers><queue-size>524288</queue-size><client-timeout>10</client-timeout><header-timeout>5</header-timeout><source-timeout>10</source-timeout></limits>
<authentication><source-password>reviewpw</source-password><relay-password>reviewpw</relay-password><admin-user>admin</admin-user><admin-password>reviewpw</admin-password></authentication>
<hostname>127.0.0.1</hostname><listen-socket><port>%d</port><bind-address>127.0.0.1</bind-address></listen-socket>
<fileserve>1</fileserve><paths><logdir>%s/log</logdir><webroot>%s/web</webroot><adminroot>%s/admin</adminroot></paths>
<logging><accesslog>access.log</accesslog><errorlog>error.log</errorlog><loglevel>4</loglevel></logging>
%s
</icecast>""" % (port, base, base, base, mounts)
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
    sources = []
    try:
        wait_port(port, process)
        for mount in ("swap", "burst", "id3", "early", "chunk", "tail", "fail",
                      "missing", "delayed"):
            sources.append(
                subprocess.Popen(
                    [
                        source_bin, "-nostdin", "-hide_banner", "-loglevel", "error",
                        "-re", "-f", "lavfi", "-i",
                        "sine=frequency=440:sample_rate=44100",
                        "-codec:a", "libmp3lame", "-b:a", "64k",
                        "-content_type", "audio/mpeg", "-f", "mp3",
                        "icecast://source:reviewpw@127.0.0.1:%d/%s" % (port, mount),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=(base / ("source-%s.log" % mount)).open("wb"),
                )
            )
        time.sleep(1.2)
        first = open_listener(port, "/swap?case=first", "IntroFirst/1")
        time.sleep(1.2)
        (intros / "intro_swap.mp3").unlink()
        (intros / "intro_swap.mp3").symlink_to("intro_swap_g2.mp3")
        process.send_signal(signal.SIGHUP)
        wait_for(
            lambda: "intro token for /swap is \"g2\"" in
            (base / "log" / "error.log").read_text(errors="replace"),
            10, "g2 intro reload")
        second = open_listener(port, "/swap?case=second", "IntroSecond/1")
        time.sleep(3.2)
        close_listener(first, False)
        time.sleep(2.0)
        close_listener(second, False)

        burst = open_listener(port, "/burst?case=burst", "IntroBurst/1")
        read_body(burst, burst_size + 2048, 8)
        close_listener(burst, False)

        id3 = open_listener(port, "/id3?case=id3", "IntroId3/1")
        read_body(id3, id3_size + 2048, 8)
        close_listener(id3, False)

        iceblocks = open_listener(port, "/burst?case=blocks", "IntroBlocks/1",
                                  headers={"IceBlocks": "1"})
        read_body(iceblocks, burst_size + 2048, 8)
        time.sleep(0.5)
        close_listener(iceblocks, False)

        tail_blocks = open_listener(port, "/tail?case=tailblocks", "IntroTailBlocks/1",
                                    headers={"IceBlocks": "1"})
        read_body(tail_blocks, tail_size + 2048, 8)
        time.sleep(0.5)
        close_listener(tail_blocks, False)

        tail_flv = open_listener(port, "/tail?case=tailflv&type=.flv", "IntroTailFlv/1")
        read_body(tail_flv, tail_size + 4096, 8)
        time.sleep(0.5)
        close_listener(tail_flv, False)

        early = open_listener(port, "/early?case=early", "IntroEarly/1")
        read_body(early, early_size + 4096, 10)
        time.sleep(0.5)
        close_listener(early, False)

        chunk = open_listener(port, "/chunk?case=chunk", "IntroChunk/1",
                              version="1.1", rcvbuf=2048)
        read_body(chunk, 1024, 2)
        chunk.shutdown(socket.SHUT_WR)
        time.sleep(2.5)
        close_listener(chunk, False)

        failing = []
        for index in range(3):
            sock = open_listener(port, "/fail?case=fail%d" % index,
                                 "IntroFail%d/1" % index)
            read_body(sock, 1024, 1)
            failing.append(sock)
        for sock in failing:
            close_listener(sock, True)

        missing = open_listener(port, "/missing?case=missing", "IntroMissing/1")
        time.sleep(0.5)
        close_listener(missing, False)

        delayed = open_listener(port, "/delayed?case=delayed", "IntroDelayed/1")
        time.sleep(2.5)
        close_listener(delayed, False)

        expected = {
            "/swap": 2, "/burst": 2, "/id3": 1, "/early": 1, "/chunk": 1,
            "/tail": 2, "/fail": 3, "/missing": 1, "/delayed": 1,
        }
        for mount, count in expected.items():
            wait_for(lambda m=mount, c=count: len(mount_records(m, "listener_remove")) == c,
                     25, "%s removes" % mount)

        def by_agent(agent):
            with record_lock:
                return next(
                    item for item in records
                    if item.get("agent") == agent
                    and item.get("action") == "listener_remove"
                )

        first_record = by_agent("IntroFirst/1")
        second_record = by_agent("IntroSecond/1")
        burst_record = by_agent("IntroBurst/1")
        id3_record = by_agent("IntroId3/1")
        blocks_record = by_agent("IntroBlocks/1")
        tail_blocks_record = by_agent("IntroTailBlocks/1")
        tail_flv_record = by_agent("IntroTailFlv/1")
        early_record = by_agent("IntroEarly/1")
        chunk_record = by_agent("IntroChunk/1")
        fail_records = [by_agent("IntroFail%d/1" % index) for index in range(3)]
        missing_record = by_agent("IntroMissing/1")
        delayed_record = by_agent("IntroDelayed/1")

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
        pinned = (
            first_record.get("intro_token") == "g1"
            and second_record.get("intro_token") == "g2"
            and first_record.get("intro_result") == "complete"
            and second_record.get("intro_result") == "complete"
        )
        burst_ok = (
            burst_record.get("intro_result") == "complete"
            and int(burst_record.get("intro_bytes") or 0) == burst_size
            and 0 < int(burst_record.get("intro_elapsed_ms") or 0) < 8000
            and burst_record.get("intro_end_ms") != ""
        )
        id3_ok = (
            id3_record.get("intro_result") == "partial"
            and int(id3_record.get("intro_bytes") or 0) == id3_size - id3_tag
            and id3_record.get("intro_end_ms") != ""
        )
        blocks_ok = (
            blocks_record.get("intro_result") == "complete"
            and int(blocks_record.get("intro_bytes") or 0) == burst_size
            and blocks_record.get("intro_end_ms") != ""
        )
        tail_ok = all(
            record.get("intro_result") == "partial"
            and int(record.get("intro_bytes") or 0) == tail_size - 1
            and record.get("intro_end_ms") != ""
            for record in (tail_blocks_record, tail_flv_record)
        )
        early_ok = (
            early_record.get("intro_result") == "partial"
            and 0 < int(early_record.get("intro_bytes") or 0) < early_size
            and early_record.get("intro_end_ms") != ""
            and int(early_record.get("intro_elapsed_ms") or 0) > 0
        )
        chunk_ok = (
            chunk_record.get("intro_result") == "partial"
            and 0 < int(chunk_record.get("intro_bytes") or 0) < huge_size
            and chunk_record.get("intro_end_ms") == ""
        )
        fail_results = [item.get("intro_result") for item in fail_records]
        fail_ok = (
            "error" in fail_results
            and all(result in ("error", "partial", "none") for result in fail_results)
            and all(item.get("intro_token") == "huge" for item in fail_records)
        )
        missing = (
            missing_record.get("intro_token") == ""
            and missing_record.get("intro_result") in ("none", "error")
        )
        delayed_ok = (
            delayed_record.get("intro_token") == "short"
            and delayed_record.get("intro_result") == "complete"
            and int(delayed_record.get("intro_end_ms") or 0) >= 2000
            and int(delayed_record.get("intro_elapsed_ms") or 0) >= 900
        )
        print(
            "intro_tokens=%s/%s pinned=%s burst=%s/%s id3=%s/%s blocks=%s/%s "
            "early=%s/%s chunk=%s/%s tail=%s/%s,%s/%s tail_ok=%s "
            "fail=%s missing=%s delayed=%s/%s sanitizer=%s"
            % (first_record.get("intro_token"), second_record.get("intro_token"),
               pinned, burst_record.get("intro_result"),
               burst_record.get("intro_bytes"), id3_record.get("intro_result"),
               id3_record.get("intro_bytes"), blocks_record.get("intro_result"),
               blocks_record.get("intro_bytes"), early_record.get("intro_result"),
               early_record.get("intro_bytes"), chunk_record.get("intro_result"),
               chunk_record.get("intro_bytes"), tail_blocks_record.get("intro_result"),
               tail_blocks_record.get("intro_bytes"),
               tail_flv_record.get("intro_result"), tail_flv_record.get("intro_bytes"),
               tail_ok, fail_results, missing_record.get("intro_result"),
               delayed_record.get("intro_result"), delayed_record.get("intro_end_ms"),
               sanitizer)
        )
        print("intro_records", first_record, second_record, burst_record,
              id3_record, blocks_record, early_record, chunk_record,
              tail_blocks_record, tail_flv_record, fail_records,
              missing_record, delayed_record)
        if process.returncode != 0 or sanitizer or not all(
                (pinned, burst_ok, id3_ok, blocks_ok, early_ok, chunk_ok, tail_ok,
                 fail_ok, missing, delayed_ok)):
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
    return run_intros(source_bin, icecast_bin)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--icecast", required=True)
    parser.add_argument("--source", default="ffmpeg")
    args = parser.parse_args()
    return run(args.source, args.icecast)


if __name__ == "__main__":
    sys.exit(main())
