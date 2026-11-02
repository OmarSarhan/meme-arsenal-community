"""Process proof also executed with the actual installed image in community CI."""
import os
from pathlib import Path
import subprocess
import sys
import signal
import time


PROCESS_CHECK = r'''
import http.client, json, os, pathlib, signal, socket, subprocess, sys, tempfile, threading, time
import uvicorn, inspect, hashlib
print('Resolved Uvicorn:', uvicorn.__version__, 'shutdown source SHA256:', hashlib.sha256(inspect.getsource(uvicorn.Server.shutdown).encode()).hexdigest(), flush=True)
SERVER = r"""
import pathlib, subprocess, sys, time
from fastapi import FastAPI, Depends
from uvicorn import Config
import meme_arsenal.drain as drain
root = pathlib.Path(sys.argv[1])
drain.STATE = root / 'proof.json'
app = FastAPI()
def dependency():
    yield
    (root / 'teardown').write_text('done')
@app.get('/')
def blocked(value=Depends(dependency)):
    (root / 'entered').touch()
    while not (root / 'release').exists():
        time.sleep(.01)
    subprocess.run([sys.executable, '-c', 'import pathlib,sys; pathlib.Path(sys.argv[1]).touch()', str(root / 'child-done')], check=True)
    return {'done': True}
drain.DrainServer(Config(app, uds=str(root / 'server.sock'), timeout_graceful_shutdown=None)).run()
"""
with tempfile.TemporaryDirectory(prefix='api-drain-') as folder:
    root = pathlib.Path(folder)
    server = subprocess.Popen([sys.executable, '-c', SERVER, folder])
    def until(predicate):
        deadline = time.monotonic() + 20
        while not predicate():
            assert server.poll() is None, server.returncode
            assert time.monotonic() < deadline, 'process barrier timed out'
            time.sleep(.01)
    client_errors = []
    def request():
        try:
            sock = socket.socket(socket.AF_UNIX)
            sock.connect(str(root / 'server.sock'))
            client = http.client.HTTPConnection('localhost')
            client.sock = sock
            client.request('GET', '/')
            assert client.getresponse().read() == b'{"done":true}'
            client.close()
        except BaseException as error:
            client_errors.append(error)
    try:
        until(lambda: (root / 'proof.json').exists() and json.loads((root / 'proof.json').read_text())['phase'] == 'ready')
        token = json.loads((root / 'proof.json').read_text())['token']
        thread = threading.Thread(target=request)
        thread.start()
        until(lambda: (root / 'entered').exists())
        server.send_signal(signal.SIGTERM)
        time.sleep(.3)
        assert server.poll() is None
        assert json.loads((root / 'proof.json').read_text()) == {'phase': 'ready', 'token': token}
        (root / 'release').touch()
        thread.join(20)
        assert not thread.is_alive() and not client_errors, client_errors
        assert server.wait(timeout=20) in (0, -signal.SIGTERM)
        assert (root / 'child-done').exists() and (root / 'teardown').exists()
        assert json.loads((root / 'proof.json').read_text()) == {'phase': 'drained', 'token': token}
    finally:
        (root / 'release').touch()
        if server.poll() is None:
            server.send_signal(signal.SIGTERM)
            server.wait(timeout=20)
print('Installed API TERM waited for synchronous request, child and yielded dependency teardown')
'''


def test_api_process_drain(tmp_path):
    env = {'PATH': os.environ['PATH'], 'HOME': str(tmp_path),
           'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    subprocess.run([sys.executable, '-c', PROCESS_CHECK], cwd=tmp_path, env=env, check=True, timeout=60)


def test_worker_stops_after_current_operation(monkeypatch):
    from threading import Event
    from meme_arsenal import online_worker as worker
    stopped = Event()
    calls = []
    monkeypatch.setattr(worker, 'require_owner', lambda _: None)
    monkeypatch.setattr(worker, '_recover_stale_jobs', lambda _: None)
    monkeypatch.setattr(worker, '_ai_work_paused', lambda _: False)
    monkeypatch.setattr(worker, '_pop_redis_job', lambda _: 'fixture')
    def process(*_, stop):
        assert stop is stopped
        assert not stop.is_set()
        stop.set()
        calls.append('completed')
    monkeypatch.setattr(worker, 'process_remix_job', process)
    worker.run_worker(None, stop=stopped)
    assert calls == ['completed']


def wait_file(path, process):
    deadline = time.monotonic() + 15
    while not path.exists():
        assert process.poll() is None
        assert time.monotonic() < deadline
        time.sleep(.01)


def test_sync_term_waits_active_child_and_reaps_idle_sleep(tmp_path):
    script = Path(__file__).resolve().parents[1] / 'docker/community-sync.sh'
    runner = tmp_path / 'sync.sh'
    runner.write_text(script.read_text().replace('/tmp/sync-', str(tmp_path / 'sync-')))
    arsenal = tmp_path / 'arsenal'
    arsenal.write_text('#!/bin/sh\ntouch "$HOME/entered"\nwhile [ ! -f "$HOME/release" ]; do sleep .01; done\ntouch "$HOME/finished"\nexit 7\n')
    arsenal.chmod(0o700)
    env = {'PATH': str(tmp_path) + ':/usr/bin:/bin', 'HOME': str(tmp_path)}
    process = subprocess.Popen(['sh', str(runner)], env=env, cwd=tmp_path)
    try:
        wait_file(tmp_path / 'entered', process)
        process.send_signal(signal.SIGTERM)
        time.sleep(.1)
        assert process.poll() is None
        (tmp_path / 'release').touch()
        assert process.wait(timeout=10) == 0
        assert (tmp_path / 'finished').exists() and (tmp_path / 'sync-failed').exists()
        assert not (tmp_path / 'sync-ok').exists()
    finally:
        (tmp_path / 'release').touch()
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=10)
    # Successful child followed by idle sleep; TERM wakes and reaps it promptly.
    arsenal.write_text('#!/bin/sh\nexit 0\n')
    process = subprocess.Popen(['sh', str(runner)], env=env, cwd=tmp_path)
    try:
        wait_file(tmp_path / 'sync-ok', process)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=5)


def test_worker_cli_joins_explanation_after_signal(tmp_path):
    code = '''
import pathlib, time
from meme_arsenal import cli, online_worker as worker
root = pathlib.Path('.')
cli._settings = lambda _: None
def explanation():
    (root / 'explaining').touch()
    while not (root / 'release-explanation').exists(): time.sleep(.01)
    (root / 'explanation-done').touch()
def run(*args, stop, **kwargs):
    worker._REJECTION_EXPLANATION_EXECUTOR.submit(explanation)
    (root / 'working').touch()
    stop.wait()
    (root / 'job-done').touch()
worker.run_worker = run
cli.online_worker_command(config=None, poll_seconds=1, once=False)
'''
    env = {'PATH': os.environ['PATH'], 'HOME': str(tmp_path),
           'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    process = subprocess.Popen([sys.executable, '-c', code], cwd=tmp_path, env=env)
    try:
        wait_file(tmp_path / 'working', process)
        wait_file(tmp_path / 'explaining', process)
        process.send_signal(signal.SIGTERM)
        wait_file(tmp_path / 'job-done', process)
        assert process.poll() is None
        (tmp_path / 'release-explanation').touch()
        assert process.wait(timeout=10) == 0
        assert (tmp_path / 'explanation-done').exists()
    finally:
        (tmp_path / 'release-explanation').touch()
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=10)
