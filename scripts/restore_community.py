#!/usr/bin/env python3
"""Restore a trusted complete v1 set into an absent, disposable local instance."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import tarfile
import time
from urllib.parse import urlsplit, unquote

ROOT = Path(__file__).resolve().parents[1]
EMPTY_SQL = """SELECT count(*) FROM (
 SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%'
 UNION ALL SELECT 1 FROM pg_namespace WHERE nspname NOT IN
 ('public','pg_catalog','information_schema') AND nspname NOT LIKE 'pg_toast%' AND nspname NOT LIKE 'pg_temp%'
 UNION ALL SELECT 1 FROM pg_extension WHERE extname <> 'plpgsql'
 UNION ALL SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public'
 UNION ALL SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()
) objects"""


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'Duplicate JSON key')
        result[key] = value
    return result


def overlap(a, b):
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def safe_name(name):
    require(isinstance(name, str) and name and '\\' not in name
            and not name.startswith('/') and all(p not in ('', '.', '..') for p in name.split('/')),
            'Unsafe archive/component path')
    return PurePosixPath(name)


def archive_members(path, roots):
    seen = {}
    with tarfile.open(path) as archive:
        for member in archive:
            name = str(safe_name(member.name))
            require(any(name == r or name.startswith(r + '/') for r in roots), 'Unexpected archive root')
            require(name not in seen and not member.mode & 0o6000, 'Duplicate or setid member')
            require(member.isdir() or member.isfile() or member.islnk(), 'Unsupported archive member')
            require(all(seen.get(str(p), 'dir') == 'dir' for p in PurePosixPath(name).parents
                        if str(p) != '.'), 'Archive ancestor conflict')
            require(member.isdir() or not any(p.startswith(name + '/') for p in seen), 'Archive ancestor conflict')
            if member.islnk():
                link = str(safe_name(member.linkname))
                require(seen.get(link) == 'file', 'Hardlink must target earlier regular member')
            if member.isfile():
                with archive.extractfile(member) as stream:
                    while stream.read(1024 * 1024):
                        pass
            seen[name] = 'dir' if member.isdir() else 'file' if member.isfile() else 'link'
    require(all(seen.get(r) == 'dir' for r in roots), 'Missing archive root')
    return seen


def complete_set(path):
    require(path.is_dir() and not path.is_symlink(), 'Backup must be a regular directory')
    marker = path / 'complete.json'
    require(marker.is_file() and not marker.is_symlink(), 'Missing complete marker')
    value = json.loads(marker.read_text(), object_pairs_hook=unique)
    require(value.get('format') == 'community-backup-v1' and value.get('complete') is True
            and value.get('resumed') is True and value.get('redis') == 'fresh-on-restore'
            and re.fullmatch(r'community-[0-9a-f]{32}', value.get('set_id', ''))
            and value['set_id'] == path.name
            and re.fullmatch(r'[A-Za-z0-9_]+', value.get('schema_revision', '')), 'Unsupported complete marker')
    recipes = value.get('recipes')
    require(isinstance(recipes, list) and recipes and recipes == list(range(len(recipes))), 'Invalid recipes')
    expected = {'database.dump', 'reserve.tar.gz', 'protected.tar.gz'} | {f'recipes/{n}.yml' for n in recipes}
    require(set(value['components']) == expected, 'Unexpected components')
    actual = set()
    for file in path.rglob('*'):
        require(not file.is_symlink() and (file.is_file() or file.is_dir()), 'Unsupported component type')
        if file.is_file():
            actual.add(str(file.relative_to(path)))
    require(actual == expected | {'complete.json'}, 'Missing/extra backup payload')
    for name, info in value['components'].items():
        safe_name(name)
        file = path / name
        require(type(info['bytes']) is int and info['bytes'] >= 0 and file.stat().st_size == info['bytes'], 'Component size mismatch')
        with file.open('rb') as stream:
            require(hashlib.file_digest(stream, 'sha256').hexdigest() == info['sha256'], 'Component checksum mismatch')
    archive_members(path / 'reserve.tar.gz', ('state/reserve',))
    protected = archive_members(path / 'protected.tar.gz', ('config', 'secrets', 'state/proxy'))
    for name in ('config/app.env', 'config/compose.env', 'config/ingress/Caddyfile',
                 'config/ingress/owner.caddy', 'secrets/database_url', 'secrets/postgres_password',
                 'secrets/public_session_secret', 'secrets/owner_session_id'):
        require(protected.get(name) in ('file', 'link'), 'Missing protected file')
    return value


def extract(path, target):
    with tarfile.open(path) as archive:
        for member in archive:
            dest = target / member.name
            dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if member.isdir():
                dest.mkdir(exist_ok=True, mode=0o700)
            elif member.islnk():
                os.link(target / member.linkname, dest)
            else:
                with archive.extractfile(member) as source, dest.open('xb') as output:
                    shutil.copyfileobj(source, output)
                dest.chmod(0o600)


def credentials(target):
    def read(name):
        with (target / 'secrets' / name).open(encoding='utf-8', newline='') as stream:
            return stream.read().removesuffix('\n')
    owner = read('owner_session_id')
    require(owner and len(owner) <= 64 and owner == owner.strip() and not any(c in owner for c in '\x00\r\n'), 'Invalid owner representation')
    password = read('postgres_password')
    try:
        url = urlsplit(read('database_url'))
        valid = (url.scheme == 'postgresql+psycopg' and url.hostname == 'db' and url.port == 5432
                 and url.username == 'meme_arsenal' and url.path == '/meme_arsenal'
                 and not url.query and not url.fragment and password and password == password.strip()
                 and not any(c in password for c in '\r\n\x00') and unquote(url.password or '') == password)
    except ValueError:
        valid = False
    require(valid, 'Unsupported or unpaired local database credentials')
    require(len(read('public_session_secret')) >= 32, 'Invalid signing material')
    return owner


def retained_files(reserve, media, jobs, keyframes):
    details = {'missing_originals': [], 'missing_results': [], 'regenerable': [], 'unavailable': []}
    def exists(value, draft=False):
        if not value or not isinstance(value, str) or '\\' in value:
            return False
        if value.startswith('/reserve/'):
            value = value[len('/reserve/'):]
        elif value.startswith('/'):
            return False
        if '..' in value.split('/'):
            return False
        candidates = [reserve / value]
        if draft:
            candidates.append(reserve / 'data/remix-drafts' / Path(value).name)
        return any(p.resolve().is_relative_to(reserve.resolve()) and p.is_file() for p in candidates)
    by_id = {m['id']: m for m in media}
    for row in media:
        if row['public_status'] == 'deleted' or not row['original_path']:
            details['unavailable'].append(row['id'])
            continue
        if not exists(row['original_path']):
            details['missing_originals'].append(row['id'])
        else:
            for field in ('thumbnail_path', 'preview_path', 'hover_preview_path'):
                if row.get(field) and not exists(row[field]):
                    details['regenerable'].append([row['id'], field])
    for row in keyframes:
        if row.get('path') and not exists(row['path']):
            details['regenerable'].append(['keyframe', row['id']])
    for row in jobs:
        if row['status'] not in ('ready', 'saving', 'saved'):
            continue
        saved = by_id.get(row.get('saved_media_item_id'), {})
        candidates = [saved.get(k) for k in ('preview_path', 'thumbnail_path')]
        if saved.get('public_original_allowed'):
            candidates.append(saved.get('original_path'))
        candidates.append(((row.get('result_json') or {}).get('result') or {}).get('image_path'))
        if not any(exists(c, True) for c in candidates):
            details['missing_results'].append(row['id'])
    return details


class Restore:
    def __init__(self, args):
        self.args = args
        self.cancelled = False
        self.target = Path(args.instance)
        self.backup = Path(args.backup_set)
        self.env = {'PATH': os.environ['PATH'], 'HOME': os.environ.get('HOME', '/root'),
                    'DOCKER_HOST': 'unix:///var/run/docker.sock'}
        self.record = {'project': args.project, 'instance': str(self.target), 'phase': 'preflight',
                       'db': None, 'status': 'unvalidated; possibly running if start attempted'}
        self.created = False

    def check(self):
        require(not self.cancelled, 'Cancelled/unvalidated; restore may already have committed')

    def run(self, *command, stdin=None, cleanup=False):
        if not cleanup:
            self.check()
        result = subprocess.run(command, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=self.env, cwd=ROOT, start_new_session=True)
        require(result.returncode == 0, 'Restore command failed (details suppressed)')
        if not cleanup:
            self.check()
        return result.stdout

    def docker(self, *command, **kwargs):
        return self.run('docker', *command, **kwargs)

    def phase(self, name):
        self.record['phase'] = name
        if self.created:
            file = self.target / 'restore-report.json'
            file.write_text(json.dumps(self.record, indent=2))
            file.chmod(0o600)

    def sql(self, query):
        return self.docker('exec', self.record['db'], 'psql', '-X', '-v', 'ON_ERROR_STOP=1',
                           '-U', 'meme_arsenal', '-d', 'meme_arsenal', '-Atc',
                           'BEGIN READ ONLY; ' + query + '; COMMIT').decode().removesuffix('\n').split('\n')[1:-1]

    def empty_catalog(self):
        require(self.sql(EMPTY_SQL) == ['0'], 'Target catalog is not empty')

    def inspect(self, cleanup=False):
        item = json.loads(self.docker('inspect', self.record['db'], cleanup=cleanup))[0]
        require(item['Id'] == self.record['db'] and item['Image'] == self.record['db_image']
                and item['Config']['Labels']['com.docker.compose.project'] == self.args.project
                and item['Config']['Labels']['com.docker.compose.service'] == 'db', 'DB identity changed')
        mounts = {m['Destination']: m['Source'] for m in item['Mounts']}
        require(mounts.get('/var/lib/postgresql/data') == str(self.target / 'state/postgres-data')
                and not item['HostConfig']['PortBindings'], 'Unsafe runtime DB target')
        return item

    def stop(self):
        item = self.inspect(cleanup=True)
        if item['State']['Running']:
            self.docker('kill', '--signal=TERM', self.record['db'], cleanup=True)
        deadline = time.monotonic() + self.args.timeout
        while True:
            state = self.inspect(cleanup=True)['State']
            if not state['Running']:
                require(state['ExitCode'] == 0 and not state.get('OOMKilled'), 'DB clean stop unproved')
                self.record['status'] = 'DB cleanly stopped; unvalidated'
                return
            require(time.monotonic() < deadline, 'DB possibly running; stop observation timed out')
            time.sleep(.2)

    def execute(self):
        require(self.target.is_absolute() and self.backup.is_absolute(), 'Absolute paths required')
        require(not self.target.exists() and not self.target.is_symlink(), 'Target must be absent')
        require(self.target.parent.resolve(strict=True) == self.target.parent, 'Canonical existing parent required')
        self.backup = self.backup.resolve(strict=True)
        require(not any(overlap(self.target, p) for p in (ROOT, self.backup.parent)), 'Target overlaps source/backup')
        require(re.fullmatch(r'[a-z0-9][a-z0-9_-]+', self.args.project), 'Invalid project')
        require(re.fullmatch(r'[A-Za-z0-9_./:@-]+', self.args.image)
                and re.fullmatch(r'/[A-Za-z0-9_./-]+', str(self.target)), 'Unsafe Compose selection')
        manifest = complete_set(self.backup)
        recipes = [Path(p).resolve(strict=True) for p in self.args.file]
        require(not any(p.is_relative_to(self.backup) for p in recipes), 'Captured recipes are evidence only')
        for resource in ('container', 'network', 'volume'):
            require(not self.docker(resource, 'ls', '-q', '--filter', 'label=com.docker.compose.project=' + self.args.project).strip(), 'Project already has resources')
        app = json.loads(self.docker('image', 'inspect', self.args.image))[0]
        db_image = manifest['containers']['db']['image']
        db = json.loads(self.docker('image', 'inspect', db_image))[0]
        require(app['Id'] == manifest['containers']['app']['image'], 'Exact captured app image required')
        require(db['Id'] == db_image and all(i['Os'] == 'linux' and i['Architecture'] == 'amd64' for i in (app, db)), 'Unsupported image identity/platform')
        required = 1073741824
        for name in ('reserve.tar.gz', 'protected.tar.gz'):
            with tarfile.open(self.backup / name) as archive:
                required += sum(m.size for m in archive if m.isfile())
        require(shutil.disk_usage(self.target.parent).free >= required, 'Insufficient capacity lower bound')
        # Read the captured source identity without executing/sourcing it.
        with tarfile.open(self.backup / 'protected.tar.gz') as archive:
            old_env = archive.extractfile('config/compose.env').read().decode()
        old = re.search(r'^ARSENAL_INSTANCE=(/[^\n]+)$', old_env, re.M)
        require(old and not overlap(self.target, Path(old[1]).resolve()), 'Target overlaps captured source')
        self.check()
        self.target.mkdir(mode=0o700)
        self.created = True
        self.record.update(set_id=manifest['set_id'], db_image=db_image)
        self.phase('extracting')
        # Recheck immediately before extraction; operator excludes concurrent edits.
        complete_set(self.backup)
        for name in ('reserve.tar.gz', 'protected.tar.gz'):
            extract(self.backup / name, self.target)
        owner = credentials(self.target)
        for name in ('state/postgres-data', 'state/redis-data'):
            (self.target / name).mkdir(mode=0o700)
        selection = self.target / 'restore.env'
        selection.write_text(f'ARSENAL_INSTANCE={self.target}\nARSENAL_IMAGE={self.args.image}\n')
        overlay = self.target / 'restore-safe.json'
        overlay.write_text(json.dumps({'services': {
            **{s: {'environment': {'AI_PROVIDER': 'mock', 'ONLINE_EMBEDDINGS_ENABLED': 'false',
                                   'PUBLIC_SHARING_ENABLED': 'false', 'ARSENAL_INSTALL_DASHBOARD': '0'}}
               for s in ('app', 'worker', 'sync')}, 'db': {'image': db_image}}}, indent=2))
        self.compose = ['docker', 'compose', '-p', self.args.project, '--env-file', str(selection)]
        for recipe in [*recipes, overlay]:
            self.compose.extend(['-f', str(recipe)])
        config = json.loads(self.run(*self.compose, '--profile', 'telegram', 'config', '--format', 'json'))
        # Sync remains excluded from restore/activation. Its profile declares
        # Telegram files even for instances that have never enabled Telegram.
        for name in ('app', 'worker'):
            environment = config['services'][name]['environment']
            for key, value in environment.items():
                if key.endswith('_FILE'):
                    require(isinstance(value, str) and value.startswith('/run/secrets/')
                            and '/' not in value[len('/run/secrets/'):]
                            and (self.target / 'secrets' / value[len('/run/secrets/'):]).is_file(),
                            'Unsupported or missing protected secret reference')
            require(not environment.get('DATABASE_URL') and not environment.get('OWNER_SESSION_ID'),
                    'Use captured core secret files without inline overrides')
            require(environment.get('DATABASE_URL_FILE') == '/run/secrets/database_url'
                    and environment.get('OWNER_SESSION_ID_FILE') == '/run/secrets/owner_session_id',
                    'Core secret selection changed')
        service = config['services']['db']
        mounts = {v['target']: v for v in service['volumes']}
        require(service['image'] == db_image and not service.get('ports')
                and not service.get('network_mode')
                and all(not config['networks'][n].get('external') for n in service.get('networks', {}))
                and set(mounts) == {'/var/lib/postgresql/data', '/run/secrets/postgres_password'}
                and all(v['type'] == 'bind' for v in mounts.values())
                and mounts['/var/lib/postgresql/data']['source'] == str(self.target / 'state/postgres-data')
                and mounts['/run/secrets/postgres_password']['source'] == str(self.target / 'secrets/postgres_password')
                and service['environment'] == {'POSTGRES_DB': 'meme_arsenal', 'POSTGRES_USER': 'meme_arsenal',
                                               'POSTGRES_PASSWORD_FILE': '/run/secrets/postgres_password'}, 'Unsafe effective DB selection')
        self.phase('starting DB; identity may need manual inspection')
        self.run(*self.compose, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'db')
        self.record['db'] = self.run(*self.compose, 'ps', '-aq', 'db').decode().strip()
        self.phase('waiting for DB')
        deadline = time.monotonic() + self.args.timeout
        while self.inspect()['State'].get('Health', {}).get('Status') != 'healthy':
            require(time.monotonic() < deadline, 'DB readiness timed out')
            time.sleep(.2)
        self.phase('empty catalog proof')
        version = self.sql("SELECT current_setting('server_version_num')::integer")
        require(len(version) == 1 and 160000 <= int(version[0]) < 170000, 'PostgreSQL 16 required')
        self.empty_catalog()
        with (self.backup / 'database.dump').open('rb') as source:
            self.docker('exec', '-i', self.record['db'], 'pg_restore', '--list', stdin=source)
        self.phase('restoring; commit possible')
        with (self.backup / 'database.dump').open('rb') as source:
            self.docker('exec', '-i', self.record['db'], 'pg_restore', '--username=meme_arsenal',
                        '--dbname=meme_arsenal', '--single-transaction', '--exit-on-error',
                        '--no-owner', '--no-privileges', stdin=source)
        self.phase('validating committed restore')
        require(self.sql('SELECT version_num FROM alembic_version') == [manifest['schema_revision']], 'Schema mismatch')
        revisions = []
        for file in (ROOT / 'migrations/versions').glob('*.py'):
            text = file.read_text()
            rev = re.search(r'^revision(?::[^=]+)?\s*=\s*[\x27\x22]([^\x27\x22]+)', text, re.M)
            down = re.search(r'^down_revision(?::[^=]+)?\s*=\s*[\x27\x22]([^\x27\x22]+)', text, re.M)
            if rev:
                revisions.append((rev[1], down[1] if down else None))
        require(set(r for r, _ in revisions) - set(d for _, d in revisions) == {manifest['schema_revision']}, 'Matching-source schema head required')
        require(self.sql("SELECT count(*) FROM pg_extension WHERE extname='vector'") == ['1'], 'Vector extension missing')
        def rows(table):
            return json.loads('\n'.join(self.sql(f"SELECT coalesce(json_agg(t), '[]') FROM {table} t")))
        require(any(r['id'] == owner for r in rows('anonymous_sessions')), 'Exact owner row missing')
        self.record['files'] = retained_files(self.target / 'state/reserve', rows('media_items'), rows('remix_jobs'), rows('media_keyframes'))
        self.phase('retained file validation')
        require(not self.record['files']['missing_originals'] and not self.record['files']['missing_results'], 'Retained bytes missing; inspect private report')
        self.stop()
        self.check()
        self.record['status'] = 'dormant validated restore; no application services started'
        self.phase('complete')
        self.check()
        print('Dormant restore complete; file category counts: ' + json.dumps({k: len(v) for k, v in self.record['files'].items()}))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('backup-set', 'instance', 'project', 'image'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('-f', '--file', action='append', required=True, help='Reviewed matching-source recipes in order')
    parser.add_argument('--operator-writers-stopped', action='store_true', required=True)
    parser.add_argument('--timeout', type=float, default=180)
    args = parser.parse_args()
    require(args.timeout > 0, 'Positive timeout required')
    restore = Restore(args)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: setattr(restore, 'cancelled', True))
    try:
        restore.execute()
    except Exception as error:
        reason = str(error) if isinstance(error, RuntimeError) else 'Input or command validation failed (details suppressed)'
        restore.record['failure'] = reason
        restore.record['failure_type'] = type(error).__name__
        if restore.created and hasattr(restore, 'compose') and not restore.record['db']:
            try:
                restore.record['db'] = restore.run(*restore.compose, 'ps', '-aq', 'db', cleanup=True).decode().strip() or None
            except Exception:
                pass
        if restore.record['db']:
            try:
                restore.stop()
            except Exception:
                restore.record['status'] = 'unvalidated; DB possibly running; inspect exact recorded identity'
        restore.phase(restore.record['phase'])
        parser.exit(1, f"Restore phase: {restore.record['phase']} ({type(error).__name__}). "
                    + ('Cancelled/unvalidated; commit possible. ' if restore.cancelled else 'Restore failed/unvalidated: ' + reason + '. ')
                    + 'Inspect private target restore-report.json; retry requires a new target/project.\n')


if __name__ == '__main__':
    main()
