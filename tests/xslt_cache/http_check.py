#!/usr/bin/env python3
"""Check concurrent XSL requests and cache eviction on a local rebuilt Icecast.

Usage: python3 tests/xslt_cache/http_check.py --icecast /path/to/icecast
Requires ffmpeg. Uses temporary files and a loopback port; no station DB.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

parser = argparse.ArgumentParser()
parser.add_argument('--icecast', required=True)
args = parser.parse_args()
xsl = '''<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"><xsl:output method="text"/><xsl:template match="/icestats"><xsl:for-each select="source"><xsl:value-of select="@mount"/>:<xsl:value-of select="listeners"/>:<xsl:value-of select="listener_peak"/></xsl:for-each></xsl:template></xsl:stylesheet>'''
with tempfile.TemporaryDirectory(prefix='icecast-status-http-') as tmp:
    root = Path(tmp)
    for name in ('web', 'admin', 'log'):
        (root/name).mkdir()
    for i in range(30):
        (root/'web'/f'status{i}.xsl').write_text(xsl)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    (root/'icecast.xml').write_text(f'''<icecast>
<limits><clients>100</clients><sources>2</sources><workers>4</workers></limits>
<authentication><source-password>test-source</source-password><admin-user>admin</admin-user><admin-password>test-admin</admin-password></authentication>
<hostname>127.0.0.1</hostname><listen-socket><bind-address>127.0.0.1</bind-address><port>{port}</port></listen-socket>
<fileserve>1</fileserve><paths><logdir>{root}/log</logdir><webroot>{root}/web</webroot><adminroot>{root}/admin</adminroot></paths>
<logging><accesslog>access.log</accesslog><errorlog>error.log</errorlog><loglevel>3</loglevel></logging>
</icecast>''')
    output = (root/'stdout.log').open('wb')
    server = subprocess.Popen([args.icecast,'-c',str(root/'icecast.xml')],stdout=output,stderr=subprocess.STDOUT)
    source = None
    def fetch(path):
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/{path}',timeout=5) as r:
            return r.read()
    try:
        deadline = time.monotonic()+10
        while True:
            try:
                fetch('status0.xsl'); break
            except OSError:
                assert server.poll() is None, (root/'stdout.log').read_text()
                if time.monotonic()>deadline:raise
                time.sleep(.05)
        source = subprocess.Popen(['ffmpeg','-nostdin','-loglevel','error','-re','-f','lavfi',
            '-i','sine=frequency=440:sample_rate=44100','-c:a','libmp3lame','-b:a','128k',
            '-content_type','audio/mpeg','-f','mp3',f'icecast://source:test-source@127.0.0.1:{port}/stream'],
            stdout=subprocess.DEVNULL,stderr=output)
        deadline=time.monotonic()+10
        while not fetch('status0.xsl').startswith(b'/stream:'):
            assert source.poll() is None
            assert time.monotonic()<deadline
            time.sleep(.05)
        def probe(i):
            body=fetch(f'status{i%30}.xsl')
            assert body.startswith(b'/stream:'), body
        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(probe,range(600)))
        for i in range(30):
            try:fetch(f'missing{i}.xsl')
            except urllib.error.HTTPError as e:assert e.code==404
            else:raise AssertionError('missing XSL accepted')
        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(probe,range(300)))
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/stream',timeout=5) as stream:
            assert len(stream.read(4096))==4096
        assert server.poll() is None and source.poll() is None
        print('PASS: 900 concurrent status probes, 30 cache entries, missing-file failures, continued audio')
    except BaseException:
        print((root/'stdout.log').read_text(errors='replace')[-3000:])
        raise
    finally:
        for process in (source,server):
            if process is not None:
                process.terminate()
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:process.kill();process.wait()
        output.close()
