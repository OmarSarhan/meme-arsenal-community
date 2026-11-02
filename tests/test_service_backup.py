"""Tiny offline fixtures: never contact Docker or load operator configuration."""
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


spec = importlib.util.spec_from_file_location('backup', Path(__file__).resolve().parents[1] / 'scripts/backup_community.py')
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


@pytest.fixture
def fixture(tmp_path):
    instance = tmp_path / 'instance'
    destination = tmp_path / 'backups'
    destination.mkdir()
    for name in backup.TREES:
        (instance / name).mkdir(parents=True)
        (instance / name / 'unknown-private').write_text('secret-sentinel')
    for name in ('state/proxy/data', 'state/proxy/config', 'config/ingress'):
        (instance / name).mkdir()
    recipe = tmp_path / 'compose.yml'
    recipe.write_text('services: {}')
    args = SimpleNamespace(instance=str(instance), backup_dir=str(destination), file=[str(recipe)],
                           project='fixture', drain_timeout=.001, retention_days=None)
    return backup.Backup(args)


def test_complete_capture_private_unknown_files_and_hardlinks(fixture, monkeypatch):
    b = fixture
    monkeypatch.setattr(backup, 'capacity', lambda *_: None)
    b.record['containers']['db'] = {'id': 'db-id'}
    def run(*args, **kwargs):
        if 'pg_dump' in args:
            kwargs['stdout'].write(b'fixture-dump')
        return SimpleNamespace(stdout=b'fixture_revision')
    b.run = run
    os.link(b.instance / 'secrets/unknown-private', b.instance / 'secrets/zz-hardlink')
    old = os.umask(0o077)
    try:
        components = b.capture()
    finally:
        os.umask(old)
    assert {'database.dump', 'reserve.tar.gz', 'protected.tar.gz', 'recipes/0.yml'} == components.keys()
    for path in [b.partial, *b.partial.rglob('*')]:
        assert path.stat().st_mode & 0o077 == 0
    with backup.tarfile.open(b.partial / 'protected.tar.gz') as archive:
        assert archive.extractfile('secrets/unknown-private').read() == b'secret-sentinel'
        assert archive.getmember('secrets/zz-hardlink').islnk()
    assert 'secret-sentinel' not in json.dumps(components)
    assert not (b.partial / 'complete.json').exists()


@pytest.mark.parametrize('kind', ['symlink', 'fifo'])
def test_unsupported_links(fixture, kind):
    path = fixture.instance / 'secrets/unsupported'
    if kind == 'symlink':
        path.symlink_to('/missing-fixture')
    else:
        os.mkfifo(path)
    with pytest.raises(RuntimeError, match='regular'):
        backup.tree_bytes(path.parent)


def test_capacity_boundary(fixture, monkeypatch):
    required = sum(backup.tree_bytes(fixture.instance / p) for p in backup.TREES) + 1073741824
    monkeypatch.setattr(backup.shutil, 'disk_usage', lambda _: SimpleNamespace(free=required))
    backup.capacity(fixture.instance, fixture.destination)
    monkeypatch.setattr(backup.shutil, 'disk_usage', lambda _: SimpleNamespace(free=required - 1))
    with pytest.raises(RuntimeError, match='capacity'):
        backup.capacity(fixture.instance, fixture.destination)


def test_timeout_retains_intent_and_never_escalates(fixture):
    b = fixture
    prior = {'id': 'exact-id', 'image': 'image', 'started': 'start'}
    b.record['containers']['worker'] = prior
    commands = []
    b.run = lambda *args, **_: commands.append(args)
    b.inspect = lambda _: {'Image': 'image', 'State': {'StartedAt': 'start', 'Running': True}}
    with pytest.raises(RuntimeError, match='timeout'):
        b.stop('worker', prior)
    assert commands == [('docker', 'kill', '--signal=TERM', 'exact-id')]
    assert 'not yet proved' in json.loads(b.journal.read_text())['pending']['worker']
    assert b.recover()


def pending_drain_fixture(b, service):
    prior = {s: {'id': s, 'image': s + '-image', 'started': 'initial', 'running': True}
             for s in backup.WRITERS}
    b.record['containers'] = prior
    b.record['api_token'] = 'initial-token'
    index = backup.WRITERS.index(service)
    b.record['pending'] = {s: 'clean; restart required' for s in backup.WRITERS[:index]}
    b.record['pending'][service] = 'TERM intent; normal exit not yet proved'
    states = {s: {'Image': p['image'], 'State': {'StartedAt': 'initial',
              'Running': i >= index, 'ExitCode': 0}}
              for i, (s, p) in enumerate(prior.items())}
    starts = []
    b.inspect = lambda cid: states[cid]
    b.healthy = lambda cid: states[cid]['State']['Running']
    b.api_proof = lambda _: {'token': 'initial-token', 'phase': 'drained'}
    def start(*command, **kwargs):
        assert command[:2] == ('docker', 'start')
        starts.append(command[2])
        states[command[2]]['State']['Running'] = True
        states[command[2]]['State']['StartedAt'] = 'restarted'
    b.run = start
    backup.durable_json(b.journal, b.record)
    return states, starts


@pytest.mark.parametrize('service,late_exit', [('worker', False), ('sync', False),
                                             ('worker', True), ('app', False)])
def test_pending_drain_recovers_clean_peers(fixture, service, late_exit):
    b = fixture
    states, starts = pending_drain_fixture(b, service)
    if late_exit:
        states[service]['State']['Running'] = False
    errors = b.recover()
    if service == 'app':
        # Docker's last healthy result may outlast an app that is now draining.
        assert starts == []
        assert b.record['pending'].keys() == {'app', 'caddy'}
    else:
        assert set(starts) == set(backup.WRITERS[:backup.WRITERS.index(service)]) | (
            {service} if late_exit else set())
        assert set(b.record['pending']) == (set() if late_exit else {service})
        assert bool(errors) is not late_exit
        assert json.loads(b.journal.read_text())['pending'] == b.record['pending']
    assert not list(b.destination.glob('community-*/complete.json'))


@pytest.mark.parametrize('fault', ['abnormal_exit', 'restarted', 'stale_api', 'shared_uncertain'])
def test_unknown_drain_holds_clean_writers(fixture, fault):
    b = fixture
    service = 'app' if fault == 'stale_api' else 'worker'
    states, starts = pending_drain_fixture(b, service)
    if fault == 'abnormal_exit':
        states[service]['State'].update(Running=False, ExitCode=137)
    elif fault == 'restarted':
        states[service]['State']['StartedAt'] = 'different-process'
    elif fault == 'stale_api':
        states[service]['State']['Running'] = False
        b.api_proof = lambda _: {'token': 'old-token', 'phase': 'drained'}
    else:
        b.record['shared_uncertain'] = True
    assert b.recover()
    assert starts == []
    assert service in b.record['pending']


def test_signal_command_error_after_clean_side_effect_is_recoverable(fixture):
    b = fixture
    prior = {'id': 'worker-id', 'image': 'image', 'started': 'start', 'running': True}
    b.record['containers']['worker'] = prior
    def fail(*_, **kwargs):
        raise RuntimeError('command failure')
    b.run = fail
    b.inspect = lambda _: {'Image': 'image', 'State': {'StartedAt': 'start', 'Running': False,
                                                    'ExitCode': 0}}
    with pytest.raises(RuntimeError, match='command failure'):
        b.stop('worker', prior)
    assert b.record['pending']['worker'] == 'clean; restart required'
    running = False
    def start(*args, **kwargs):
        nonlocal running
        assert args == ('docker', 'start', 'worker-id')
        running = True
        raise RuntimeError('start failed after side effect')
    b.run = start
    b.inspect = lambda _: {'Image': 'image', 'State': {'Running': running}}
    assert b.recover() == []
    assert b.record['pending'] == {}


def test_stale_api_ack_refused(fixture):
    b = fixture
    prior = {'id': 'app-id', 'image': 'image', 'started': 'start'}
    b.record['api_token'] = 'new'
    b.run = lambda *_, **__: None
    b.inspect = lambda _: {'Image': 'image', 'State': {'StartedAt': 'start', 'Running': False,
                                                    'ExitCode': 143}}
    b.api_proof = lambda _: {'token': 'old', 'phase': 'drained'}
    with pytest.raises(RuntimeError, match='stale'):
        b.stop('app', prior)
    assert 'not yet proved' in b.record['pending']['app']


def test_retention_only_recognized_completed_sets(fixture):
    b = fixture
    for name in ('unknown', '.community-partial-old', 'community-' + 'a' * 32):
        path = b.destination / name
        path.mkdir()
        (path / 'complete.json').write_text(json.dumps({'format': 'community-backup-v1',
                                'complete': True, 'set_id': name}))
    backup.retain(b.destination, b.destination / 'new', 0)
    assert {p.name for p in b.destination.iterdir()} == {'unknown', '.community-partial-old'}


@pytest.mark.parametrize('sync_running,worker_running,fail_capture', [(None, True, False),
                         (False, False, False), (True, True, False), (None, False, True)])
def test_prior_state_and_capture_failure(fixture, monkeypatch, sync_running, worker_running, fail_capture):
    b = fixture
    monkeypatch.setattr(backup, 'capacity', lambda *_: None)
    states = {s: {'id': s + '-id', 'image': s + '-image', 'started': 'initial', 'running': True}
              for s in ('caddy', 'app', 'worker', 'db', 'redis')}
    states['worker']['running'] = worker_running
    if sync_running is not None:
        states['sync'] = {'id': 'sync-id', 'image': 'sync-image', 'started': 'initial', 'running': sync_running}
    initial = {s: dict(p) for s, p in states.items()}
    commands = []
    b.inventory = lambda: {s: dict(p) for s, p in states.items()}
    b.healthy = lambda cid: states[cid.removesuffix('-id')]['running']
    b.api_proof = lambda _: {'token': 'initial', 'phase': 'ready' if states['app']['running'] else 'drained'}
    def inspect(cid):
        p = states[cid.removesuffix('-id')]
        return {'Image': p['image'], 'State': {'StartedAt': p['started'], 'Running': p['running'], 'ExitCode': 0}}
    b.inspect = inspect
    def run(*command, **kwargs):
        commands.append(command)
        if command[:2] == ('docker', 'compose'):
            assert command[-2:] == ('config', '--quiet')
        elif command[:3] == ('docker', 'kill', '--signal=TERM'):
            states[command[3].removesuffix('-id')]['running'] = False
        elif command[:2] == ('docker', 'start'):
            states[command[2].removesuffix('-id')]['running'] = True
        elif 'pg_dump' in command:
            kwargs['stdout'].write(b'fixture-dump')
        elif 'pg_restore' not in command and 'psql' not in command:
            raise AssertionError(command)
        return SimpleNamespace(stdout=b'fixture_revision')
    b.run = run
    if fail_capture:
        def fail():
            assert not any(states[s]['running'] for s in backup.WRITERS if s in states)
            raise OSError('fixture ENOSPC')
        b.capture = fail
    old = os.umask(0o077)
    try:
        if fail_capture:
            with pytest.raises(RuntimeError, match='ENOSPC'):
                b.execute()
        else:
            b.execute()
    finally:
        os.umask(old)
    assert states == initial
    assert not b.journal.exists()
    starts = [c[2] for c in commands if c[:2] == ('docker', 'start')]
    assert set(starts) == {p['id'] for s, p in initial.items() if s in backup.WRITERS and p['running']}
    assert len(list(b.destination.glob('community-*/complete.json'))) == (0 if fail_capture else 1)


def test_second_process_same_instance_different_destination_lock(fixture):
    import subprocess
    import sys
    with (fixture.instance / '.backup.lock').open('a') as lock:
        backup.fcntl.flock(lock, backup.fcntl.LOCK_EX | backup.fcntl.LOCK_NB)
        other = fixture.destination.parent / 'different-backups'
        other.mkdir()
        result = subprocess.run([sys.executable, str(backup.ROOT / 'scripts/backup_community.py'),
                                 '--instance', str(fixture.instance), '--project', 'fixture',
                                 '--backup-dir', str(other), '-f', str(fixture.recipes[0]),
                                 '--operator-writers-stopped'], capture_output=True)
        assert result.returncode != 0 and b'Resource temporarily unavailable' in result.stderr


def test_capture_signal_latches_and_waits_owned_reader(fixture):
    import signal
    import subprocess
    import sys
    import time
    b = fixture
    code = '''
import importlib.util, pathlib, signal, sys
spec = importlib.util.spec_from_file_location('backup', sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
b = object.__new__(m.Backup); b.env = {'PATH': '/usr/bin:/bin'}; b.cancelled = False
for sig in (signal.SIGTERM, signal.SIGINT): signal.signal(sig, lambda *_: setattr(b, 'cancelled', True))
reader = "import pathlib,time; p=pathlib.Path('.'); (p/'reading').touch(); exec('while not (p/\\\"release\\\").exists(): time.sleep(.01)'); (p/'reader-done').touch()"
b.run(sys.executable, '-c', reader)
assert b.cancelled and pathlib.Path('reader-done').exists()
pathlib.Path('recovery-eligible').touch()
'''
    process = subprocess.Popen([sys.executable, '-c', code, str(backup.ROOT / 'scripts/backup_community.py')], cwd=b.destination)
    try:
        deadline = time.monotonic() + 10
        while not (b.destination / 'reading').exists():
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(.01)
        process.send_signal(signal.SIGTERM)
        time.sleep(.05)
        process.send_signal(signal.SIGINT)
        assert process.poll() is None
        (b.destination / 'release').touch()
        assert process.wait(timeout=10) == 0
        assert (b.destination / 'recovery-eligible').exists()
    finally:
        (b.destination / 'release').touch()
        if process.poll() is None:
            process.wait(timeout=10)


@pytest.mark.parametrize('fault', [None, 'mount', 'recipe', 'oneoff'])
def test_inventory_includes_profile_omitted_sync_and_checks_identity(fixture, fault):
    b = fixture
    targets = {
        'app': {'/reserve': 'state/reserve', '/run/secrets': 'secrets'},
        'worker': {'/reserve': 'state/reserve', '/run/secrets': 'secrets'},
        'sync': {'/reserve': 'state/reserve', '/run/secrets': 'secrets'},
        'caddy': {'/etc/caddy': 'config/ingress', '/data': 'state/proxy/data', '/config': 'state/proxy/config'},
        'db': {'/var/lib/postgresql/data': 'state/postgres-data'},
        'redis': {'/data': 'state/redis-data'}}
    containers = {}
    for service, mounts in targets.items():
        containers[service] = {'Id': service, 'Image': service + '-image',
            'Config': {'Labels': {'com.docker.compose.service': service,
                                 'com.docker.compose.project.config_files': str(b.recipes[0])}},
            'State': {'Running': True, 'Status': 'running', 'StartedAt': 'initial'},
            'Mounts': [{'Destination': target, 'Source': str(b.instance / source)} for target, source in mounts.items()]}
    if fault == 'mount':
        containers['sync']['Mounts'][0]['Source'] = '/different-instance'
    if fault == 'recipe':
        containers['app']['Config']['Labels']['com.docker.compose.project.config_files'] = '/wrong-recipe'
    if fault == 'oneoff':
        containers['extra'] = {'Config': {'Labels': {'com.docker.compose.oneoff': 'True'}},
                               'State': {'Running': True}}
    b.output = lambda *command: ' '.join(containers).encode()
    b.inspect = lambda cid: containers[cid]
    if fault:
        with pytest.raises(RuntimeError):
            b.inventory()
    else:
        assert b.inventory()['sync']['running'] is True
