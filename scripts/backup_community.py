#!/usr/bin/env python3
"""Offline-maintenance community backup. Never starts dependencies or kills writers."""

import argparse
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
WRITERS = ('caddy', 'app', 'worker', 'sync')
TREES = ('state/reserve', 'config', 'secrets', 'state/proxy')


def tree_bytes(root):
    total = 0
    for path in [root, *root.rglob('*')]:
        mode = path.lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise RuntimeError('Captured trees must contain only regular files/directories')
        total += path.lstat().st_size
    return total


def capacity(instance, destination):
    required = sum(tree_bytes(instance / name) for name in TREES) + 1073741824
    if shutil.disk_usage(destination).free < required:
        raise RuntimeError('Insufficient backup capacity (protected trees plus 1 GiB lower bound)')


def durable_json(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Backup:
    def __init__(self, args):
        self.args = args
        if not Path(args.instance).is_absolute() or not Path(args.backup_dir).is_absolute():
            raise RuntimeError('Instance and destination must be absolute paths')
        self.instance = Path(args.instance).resolve(strict=True)
        self.destination = Path(args.backup_dir).resolve(strict=True)
        self.recipes = [Path(p).resolve(strict=True) for p in args.file]
        self.env = {'PATH': os.environ['PATH'], 'HOME': os.environ.get('HOME', '/root')}
        self.cancelled = False
        self.journal = self.instance / '.backup-recovery.json'
        self.record = {'format': 1, 'project': args.project, 'containers': {}, 'pending': {}}
        self.partial = None

    def run(self, *command, **kwargs):
        # Signals latch; subprocess.run waits/reaps the current reader, even on a
        # second signal. Never terminate a docker-exec client and assume remote exit.
        result = subprocess.run(command, env=self.env, capture_output=False,
                                stderr=subprocess.DEVNULL, start_new_session=True, **kwargs)
        if result.returncode:
            raise RuntimeError('Backup command failed: ' + command[0])
        return result

    def output(self, *command):
        return self.run(*command, stdout=subprocess.PIPE).stdout

    def inspect(self, container):
        return json.loads(self.output('docker', 'inspect', container))[0]

    def inventory(self):
        ids = self.output('docker', 'ps', '-aq', '--filter',
                          'label=com.docker.compose.project=' + self.args.project).decode().split()
        found = {}
        for container in ids:
            item = self.inspect(container)
            labels = item['Config']['Labels']
            service = labels.get('com.docker.compose.service')
            state = item['State']
            if labels.get('com.docker.compose.oneoff', '').lower() == 'true':
                if state['Running']:
                    raise RuntimeError('Running project one-off; finish manual writers first')
                continue
            if service not in (*WRITERS, 'db', 'redis') or service in found:
                raise RuntimeError('Unexpected or scaled project container')
            selected = labels.get('com.docker.compose.project.config_files', '').split(',')
            if selected != [str(p) for p in self.recipes]:
                raise RuntimeError('Selected Compose files differ from container creation recipe')
            if state['Status'] not in ('running', 'exited', 'created') or state.get('Paused'):
                raise RuntimeError('Ambiguous container state')
            mounts = {m['Destination']: Path(m['Source']).resolve() for m in item['Mounts']}
            expected = ({'/reserve': 'state/reserve', '/run/secrets': 'secrets'}
                        if service in ('app', 'worker', 'sync') else
                        {'/etc/caddy': 'config/ingress', '/data': 'state/proxy/data',
                         '/config': 'state/proxy/config'} if service == 'caddy' else
                        {'/var/lib/postgresql/data': 'state/postgres-data'} if service == 'db'
                        else {'/data': 'state/redis-data'})
            if any(mounts.get(target) != self.instance / source for target, source in expected.items()):
                raise RuntimeError('Container mounts do not match explicit instance')
            found[service] = {'id': item['Id'], 'image': item['Image'],
                              'started': state['StartedAt'], 'running': state['Running']}
        if not {'app', 'worker', 'caddy', 'db', 'redis'} <= found.keys():
            raise RuntimeError('Required community containers missing')
        return found

    def api_proof(self, container):
        data = self.output('docker', 'cp', container + ':/tmp/arsenal-api-drain.json', '-')
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            return json.load(archive.extractfile(archive.getmembers()[0]))

    def healthy(self, container):
        item = self.inspect(container)
        return item['State']['Running'] and item['State'].get('Health', {}).get('Status') == 'healthy'

    def check_cancel(self):
        if self.cancelled:
            raise RuntimeError('Backup cancelled; current owned reader completed before recovery')

    def drained(self, service, prior):
        item = self.inspect(prior['id'])
        state = item['State']
        if item['Image'] != prior['image'] or state['StartedAt'] != prior['started']:
            raise RuntimeError('Writer restarted or changed during drain')
        if state['Running']:
            return False
        if state.get('OOMKilled') or state['ExitCode'] not in ((0, 143) if service == 'app' else (0,)):
            raise RuntimeError('Normal writer exit not proved')
        if service == 'app' and self.api_proof(prior['id']) != {
                'token': self.record['api_token'], 'phase': 'drained'}:
            raise RuntimeError('API drain acknowledgement missing or stale')
        return True

    def stop(self, service, prior):
        self.record['pending'][service] = 'TERM intent; normal exit not yet proved'
        durable_json(self.journal, self.record)
        command_error = None
        try:
            self.run('docker', 'kill', '--signal=TERM', prior['id'])
        except RuntimeError as error:
            command_error = error
        deadline = time.monotonic() + self.args.drain_timeout
        while not self.drained(service, prior):
            if time.monotonic() >= deadline:
                raise RuntimeError('TERM drain timeout; pending writer can still exit later')
            time.sleep(.2)
        self.record['pending'][service] = 'clean; restart required'
        durable_json(self.journal, self.record)
        if command_error:
            raise command_error

    def recover(self):
        errors = []
        uncertain = self.record.get('shared_uncertain', False)
        # A same-process writer still draining does not endanger clean peers.
        # Verify late exits before restarting anything; abnormal/unknown exits do.
        for service, phase in list(self.record['pending'].items()):
            if phase == 'clean; restart required':
                continue
            try:
                if self.drained(service, self.record['containers'][service]):
                    self.record['pending'][service] = 'clean; restart required'
                    durable_json(self.journal, self.record)
            except Exception as error:
                uncertain = True
                errors.append(service + ': drain verification failed: ' + str(error))
        for service in ('app', 'worker', 'sync', 'caddy'):
            if service not in self.record['pending']:
                continue
            if uncertain:
                errors.append(service + ': shared-data or writer-exit uncertainty; inspect before restarting')
                continue
            if self.record['pending'][service] != 'clean; restart required':
                errors.append(service + ': TERM drain still pending; inspect before restarting')
                continue
            prior = self.record['containers'][service]
            try:
                current = self.inspect(prior['id'])
                if current['Image'] != prior['image']:
                    raise RuntimeError('Container identity changed')
                if service == 'caddy' and self.record['containers']['app']['running']:
                    if ('app' in self.record['pending']
                            or not self.healthy(self.record['containers']['app']['id'])):
                        raise RuntimeError('Ingress requires ready app')
                if not current['State']['Running']:
                    try:
                        self.run('docker', 'start', prior['id'], stdout=subprocess.DEVNULL)
                    except RuntimeError:
                        if not self.inspect(prior['id'])['State']['Running']:
                            raise
                if service == 'app':
                    deadline = time.monotonic() + self.args.drain_timeout
                    while not self.healthy(prior['id']):
                        # Bootstrap failure is shared-data uncertainty. HTTP-only
                        # failure still permits clean independent worker recovery.
                        if not self.inspect(prior['id'])['State']['Running']:
                            uncertain = True
                            raise RuntimeError('App startup exited; inspect bootstrap/migrations')
                        if time.monotonic() >= deadline:
                            try:
                                proof = self.api_proof(prior['id'])
                                uncertain = (proof.get('phase') != 'ready'
                                             or proof.get('token') == self.record.get('api_token'))
                            except Exception:
                                uncertain = True
                            raise RuntimeError('App readiness timeout')
                        time.sleep(.2)
                del self.record['pending'][service]
                durable_json(self.journal, self.record)
            except Exception as error:
                errors.append(service + ': ' + str(error))
        return errors

    def capture(self):
        capacity(self.instance, self.destination)
        self.record['capture_started'] = time.time()
        self.partial = Path(tempfile.mkdtemp(prefix='.community-partial-', dir=self.destination))
        dump = self.partial / 'database.dump'
        db = self.record['containers']['db']['id']
        revision = self.output('docker', 'exec', db, 'psql', '-U', 'meme_arsenal',
                               '-d', 'meme_arsenal', '-Atc', 'select version_num from alembic_version').decode().strip()
        if not re.fullmatch(r'[a-zA-Z0-9_]+', revision):
            raise RuntimeError('Expected one Alembic schema revision')
        self.record['schema_revision'] = revision
        with dump.open('wb') as output:
            self.run('docker', 'exec', db, 'pg_dump', '-U', 'meme_arsenal', '-d',
                     'meme_arsenal', '-Fc', stdout=output)
        self.check_cancel()
        with dump.open('rb') as source:
            self.run('docker', 'exec', '-i', db, 'pg_restore', '--list',
                     stdin=source, stdout=subprocess.DEVNULL)
        for name, trees in (('reserve.tar.gz', ('state/reserve',)),
                            ('protected.tar.gz', ('config', 'secrets', 'state/proxy'))):
            self.check_cancel()
            with tarfile.open(self.partial / name, 'w:gz') as archive:
                for tree in trees:
                    tree_bytes(self.instance / tree)
                    archive.add(self.instance / tree, arcname=tree)
            with tarfile.open(self.partial / name) as archive:
                for member in archive:
                    if member.isfile():
                        with archive.extractfile(member) as source:
                            while source.read(1024 * 1024):
                                pass
        recipe_dir = self.partial / 'recipes'
        recipe_dir.mkdir()
        for index, recipe in enumerate(self.recipes):
            if not stat.S_ISREG(recipe.lstat().st_mode):
                raise RuntimeError('Recipe must be a regular file')
            shutil.copyfile(recipe, recipe_dir / f'{index}.yml')
        components = {}
        for path in self.partial.rglob('*'):
            if path.is_file():
                with path.open('rb') as source:
                    digest = hashlib.file_digest(source, 'sha256').hexdigest()
                with path.open('rb') as source:
                    if hashlib.file_digest(source, 'sha256').hexdigest() != digest:
                        raise RuntimeError('Checksum validation failed')
                components[str(path.relative_to(self.partial))] = {'sha256': digest, 'bytes': path.stat().st_size}
        self.record['capture_finished'] = time.time()
        return components

    def execute(self):
        if any(self.destination == p or self.destination.is_relative_to(p)
               or p.is_relative_to(self.destination) for p in (self.instance, ROOT)):
            raise RuntimeError('Destination must be outside instance and source trees without overlap')
        for tree in (*TREES, 'state/proxy/data', 'state/proxy/config', 'config/ingress'):
            if not (self.instance / tree).is_dir():
                raise RuntimeError('Required protected tree missing')
        lock = (self.instance / '.backup.lock').open('a')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            if self.journal.exists():
                raise RuntimeError('Unresolved .backup-recovery.json; reconcile recorded IDs first')
            capacity(self.instance, self.destination)
            compose_env = self.instance / 'config/compose.env'
            # Compose reads the generated file as data. Never shell-source it or
            # inherit ambient COMPOSE_*/ARSENAL_* deployment selection.
            compose = ['docker', 'compose', '-p', self.args.project, '--env-file', str(compose_env)]
            for recipe in self.recipes:
                compose.extend(['-f', str(recipe)])
            self.run(*compose, 'config', '--quiet', stdout=subprocess.DEVNULL)
            self.record['containers'] = self.inventory()
            for service in ('db', 'redis'):
                if not self.healthy(self.record['containers'][service]['id']):
                    raise RuntimeError('DB and Redis must already be healthy')
            app = self.record['containers']['app']
            if app['running']:
                if not self.healthy(app['id']):
                    raise RuntimeError('App startup not ready')
                proof = self.api_proof(app['id'])
                if proof['phase'] != 'ready':
                    raise RuntimeError('App startup proof missing')
                self.record['api_token'] = proof['token']
            durable_json(self.journal, self.record)
            primary = None
            components = None
            try:
                for service in WRITERS:
                    self.check_cancel()
                    prior = self.record['containers'].get(service)
                    if prior and prior['running']:
                        self.stop(service, prior)
                self.record['shared_uncertain'] = True
                durable_json(self.journal, self.record)
                current = self.inventory()
                if any(current[s]['id'] != p['id'] or current[s]['running']
                       for s, p in self.record['containers'].items() if s in WRITERS):
                    raise RuntimeError('Writer inventory changed')
                if any(current[s] != self.record['containers'][s] for s in ('db', 'redis')):
                    raise RuntimeError('DB/Redis process identity changed')
                self.record['shared_uncertain'] = False
                durable_json(self.journal, self.record)
                components = self.capture()
                self.check_cancel()
            except Exception as error:
                primary = error
            recovery = self.recover()
            if not recovery and not self.record['pending']:
                self.journal.unlink()
            if primary or recovery or self.cancelled:
                if self.partial:
                    shutil.rmtree(self.partial)
                if recovery:
                    print('Recovery required: ' + '; '.join(recovery))
                    for service in ('app', 'worker', 'sync', 'caddy'):
                        if service in self.record['pending']:
                            cid = self.record['containers'][service]['id']
                            print('Inspect recorded identity/drain first: docker inspect ' + shlex.quote(cid))
                            print('When safe: docker start ' + shlex.quote(cid))
                raise RuntimeError(str(primary or 'Recovery/cancellation failed'))
            set_id = 'community-' + uuid.uuid4().hex
            durable_json(self.partial / 'complete.json', {'format': 'community-backup-v1',
                         'complete': True, 'set_id': set_id, 'components': components,
                         'containers': self.record['containers'], 'redis': 'fresh-on-restore',
                         'schema_revision': self.record['schema_revision'],
                         'capture_started': self.record['capture_started'],
                         'capture_finished': self.record['capture_finished'],
                         'prior_states': {s: ('absent' if s not in self.record['containers'] else
                             'running' if self.record['containers'][s]['running'] else 'stopped') for s in WRITERS},
                         'validated': ['pg_restore-list', 'archive-read', 'sha256'],
                         'recipes': list(range(len(self.recipes))), 'resumed': True})
            final = self.destination / set_id
            self.partial.rename(final)
            print('Complete private backup: ' + str(final))
            if self.args.retention_days is not None:
                try:
                    retain(self.destination, final, self.args.retention_days)
                except Exception:
                    raise RuntimeError('Backup complete at ' + str(final) + '; retention failed') from None
        finally:
            lock.close()


def retain(destination, new, days):
    for path in destination.iterdir():
        if path == new or not re.fullmatch(r'community-[0-9a-f]{32}', path.name) or path.is_symlink():
            continue
        marker = path / 'complete.json'
        if not marker.is_file() or marker.is_symlink():
            continue
        value = json.loads(marker.read_text())
        if (value.get('format') == 'community-backup-v1' and value.get('complete') is True
                and value.get('set_id') == path.name
                and marker.stat().st_mtime < time.time() - days * 86400):
            shutil.rmtree(path)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance', required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--backup-dir', required=True, help='Existing, operator-verified mounted destination')
    parser.add_argument('-f', '--file', action='append', required=True, help='Selected Compose files, in order')
    parser.add_argument('--operator-writers-stopped', action='store_true', required=True)
    parser.add_argument('--drain-timeout', type=float, default=300)
    parser.add_argument('--retention-days', type=int)
    args = parser.parse_args()
    if args.drain_timeout <= 0 or (args.retention_days is not None and args.retention_days < 0):
        parser.error('Timeout must be positive and retention nonnegative')
    backup = Backup(args)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: setattr(backup, 'cancelled', True))
    try:
        backup.execute()
    except Exception as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
