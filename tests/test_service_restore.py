"""Isolated stdlib fixtures; no application imports, Docker or external sockets."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import tarfile
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('restore', Path(__file__).resolve().parents[1] / 'scripts/restore_community.py')
restore = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restore)


@pytest.fixture
def backup(tmp_path):
    source = tmp_path / 'source'
    for name in ('state/reserve', 'config/ingress', 'secrets', 'state/proxy/data', 'state/proxy/config'):
        (source / name).mkdir(parents=True, exist_ok=True)
    for name, content in {'config/app.env': 'AI_PROVIDER=mock', 'config/compose.env': f'ARSENAL_INSTANCE={source}\n',
                          'config/ingress/Caddyfile': 'fixture', 'config/ingress/owner.caddy': 'fixture',
                          'secrets/owner_session_id': 'Case Unicode Ω\n', 'secrets/public_session_secret': 's'*40,
                          'secrets/postgres_password': 'pass @',
                          'secrets/database_url': 'postgresql+psycopg://meme_arsenal:pass%20%40@db:5432/meme_arsenal',
                          'state/reserve/draft': 'draft'}.items():
        (source / name).write_text(content)
    os.link(source / 'state/reserve/draft', source / 'state/reserve/zz-link')
    saved = tmp_path / 'backups' / ('community-' + 'a'*32)
    saved.mkdir(parents=True)
    for name, trees in (('reserve.tar.gz', ('state/reserve',)), ('protected.tar.gz', ('config', 'secrets', 'state/proxy'))):
        with tarfile.open(saved / name, 'w:gz') as archive:
            for tree in trees:
                archive.add(source / tree, arcname=tree)
    (saved / 'database.dump').write_bytes(b'fixture')
    (saved / 'recipes').mkdir()
    (saved / 'recipes/0.yml').write_text('services: {}')
    components = {str(p.relative_to(saved)): {'bytes': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                  for p in saved.rglob('*') if p.is_file()}
    marker = dict(format='community-backup-v1', complete=True, resumed=True, redis='fresh-on-restore',
                  set_id=saved.name, schema_revision='013_explicit_recipe_grants', recipes=[0], components=components,
                  containers={'app': {'image': 'sha256:app'}, 'db': {'image': 'sha256:db'}})
    (saved / 'complete.json').write_text(json.dumps(marker))
    return saved


def test_complete_private_extraction_hardlink_and_exact_owner(backup, tmp_path):
    restore.complete_set(backup)
    target = tmp_path / 'new'
    target.mkdir(mode=0o700)
    prior = os.umask(0o077)
    try:
        for name in ('reserve.tar.gz', 'protected.tar.gz'):
            restore.extract(backup / name, target)
    finally:
        os.umask(prior)
    assert restore.credentials(target) == 'Case Unicode Ω'
    assert (target / 'secrets/owner_session_id').read_bytes() == 'Case Unicode Ω\n'.encode()
    assert (target / 'state/reserve/draft').stat().st_ino == (target / 'state/reserve/zz-link').stat().st_ino
    assert all(p.stat().st_mode & 0o077 == 0 for p in target.rglob('*'))
    (target / 'secrets/database_url').write_text('postgresql+psycopg://meme_arsenal:pass%20%40@db:5432/meme_arsenal?host=remote')
    with pytest.raises(RuntimeError, match='credentials'):
        restore.credentials(target)
    (target / 'secrets/owner_session_id').write_bytes(b'owner\r\n')
    with pytest.raises(RuntimeError, match='owner'):
        restore.credentials(target)


@pytest.mark.parametrize('change', ['checksum', 'missing', 'extra', 'duplicate'])
def test_complete_refusal(backup, change):
    if change == 'checksum':
        (backup / 'database.dump').write_bytes(b'corrupt')
    elif change == 'missing':
        (backup / 'database.dump').unlink()
    elif change == 'extra':
        (backup / 'extra').write_text('extra')
    else:
        (backup / 'complete.json').write_text('{"format":1,"format":2}')
    with pytest.raises(RuntimeError):
        restore.complete_set(backup)


@pytest.mark.parametrize('name,kind,link', [('../escape','file',''), ('/absolute','file',''),
    ('state/reserve/a','sym','outside'), ('state/reserve/a','hard','config/other'),
    ('state/reserve/a','hard','state/reserve/later'), ('config/file','file','')])
def test_archive_refusal(tmp_path, name, kind, link):
    archive_path = tmp_path / 'bad.tar.gz'
    with tarfile.open(archive_path, 'w:gz') as archive:
        member = tarfile.TarInfo('state/reserve')
        member.type = tarfile.DIRTYPE
        archive.addfile(member)
        member = tarfile.TarInfo(name)
        member.type = {'file': tarfile.REGTYPE, 'sym': tarfile.SYMTYPE, 'hard': tarfile.LNKTYPE}[kind]
        member.linkname = link
        archive.addfile(member)
    with pytest.raises(RuntimeError):
        restore.archive_members(archive_path, ('state/reserve',))
    assert not (tmp_path / 'escape').exists()


def test_retained_originals_drafts_results_and_derivatives(tmp_path):
    reserve = tmp_path
    (reserve / 'data/remix-drafts').mkdir(parents=True)
    (reserve / 'original').write_text('original')
    (reserve / 'data/remix-drafts/result.png').write_text('AI bytes')
    media = [dict(id=1, original_path='original', preview_path='missing', public_status='draft'),
             dict(id=2, original_path=None, public_status='deleted'),
             dict(id=3, original_path='/external/private', public_status='draft')]
    jobs = [dict(id='ready', status='ready', result_json={'result': {'image_path': 'result.png'}}),
            dict(id='lost', status='saved', result_json={})]
    result = restore.retained_files(reserve, media, jobs, [])
    assert result == dict(missing_originals=[3], missing_results=['lost'], regenerable=[[1, 'preview_path']], unavailable=[2])


def test_existing_target_refused_before_docker(tmp_path):
    args = SimpleNamespace(instance=str(tmp_path), backup_set=str(tmp_path), project='fixture', image='fixture', file=[])
    subject = restore.Restore(args)
    subject.docker = lambda *_: pytest.fail('Docker must not run')
    with pytest.raises(RuntimeError, match='absent'):
        subject.execute()


def test_empty_catalog_refusal_and_cancel_after_reap(tmp_path):
    args = SimpleNamespace(instance=str(tmp_path / 'new'), backup_set=str(tmp_path), project='fixture')
    subject = restore.Restore(args)
    subject.sql = lambda _: ['1']
    with pytest.raises(RuntimeError, match='not empty'):
        subject.empty_catalog()
    # Real owned child, two signals while it runs; run must reap before cancellation.
    script = "import os,signal,time; os.kill(os.getppid(),signal.SIGTERM); os.kill(os.getppid(),signal.SIGINT); time.sleep(.05)"
    previous = {sig: signal.signal(sig, lambda *_: setattr(subject, 'cancelled', True)) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        with pytest.raises(RuntimeError, match='may already have committed'):
            subject.run(sys.executable, '-c', script)
        with pytest.raises(ChildProcessError):
            os.waitpid(-1, os.WNOHANG)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def test_sql_retains_multiline_json_and_unicode_separators(tmp_path):
    args = SimpleNamespace(instance=str(tmp_path / 'new'), backup_set=str(tmp_path), project='fixture')
    subject = restore.Restore(args)
    rows = [{'id': 'one', 'text': 'first\u2028second'}, {'id': 'two'}]
    payload = json.dumps(rows, ensure_ascii=False, indent=1)
    subject.docker = lambda *_: ('BEGIN\n' + payload + '\nCOMMIT\n').encode()
    assert json.loads('\n'.join(subject.sql('synthetic aggregate'))) == rows


def test_failure_reports_phase_and_type_without_values(tmp_path, monkeypatch, capsys):
    def fail(subject):
        subject.phase('validating committed restore')
        raise ValueError('fixture-sensitive-value')
    monkeypatch.setattr(restore.Restore, 'execute', fail)
    monkeypatch.setattr(restore.signal, 'signal', lambda *_: None)
    monkeypatch.setattr(sys, 'argv', ['restore', '--instance', str(tmp_path / 'new'),
        '--backup-set', str(tmp_path / 'backup'), '--project', 'fixture', '--image', 'fixture',
        '-f', str(tmp_path / 'compose.yml'), '--operator-writers-stopped'])
    previous = os.umask(0o077)
    try:
        with pytest.raises(SystemExit) as error:
            restore.main()
    finally:
        os.umask(previous)
    assert error.value.code == 1
    output = capsys.readouterr().err
    assert 'validating committed restore (ValueError)' in output
    assert 'fixture-sensitive-value' not in output


@pytest.mark.parametrize('failure', [None, 'restore', 'postcommit', 'cancel', 'project', 'image', 'bind', 'secret', 'schema'])
def test_ordering_and_representative_failures(backup, tmp_path, monkeypatch, failure):
    # The synthetic backup is revision 013; model its matching retained source,
    # independent of today's head. Real current-head restoration runs in CI.
    source = tmp_path / 'matching-source'
    versions = source / 'migrations/versions'
    versions.mkdir(parents=True)
    (versions / '013.py').write_text("revision = '013_explicit_recipe_grants'\ndown_revision = '012_public_share_codes'\n")
    if failure == 'schema':
        (versions / '014.py').write_text("revision = '014_source_identity'\ndown_revision = '013_explicit_recipe_grants'\n")
    monkeypatch.setattr(restore, 'ROOT', source)
    recipe = tmp_path / 'reviewed.yml'
    recipe.write_text('reviewed fixture')
    target = tmp_path / 'target'
    args = SimpleNamespace(instance=str(target), backup_set=str(backup), project='new-project',
                           image='fixture:retained', file=[str(recipe)], timeout=1)
    subject = restore.Restore(args)
    events = []
    def run(*command, **kwargs):
        events.append(command)
        if 'config' in command:
            env = {'DATABASE_URL_FILE': '/run/secrets/database_url', 'OWNER_SESSION_ID_FILE': '/run/secrets/owner_session_id'}
            config = {'services': {**{n: {'environment': env} for n in ('app', 'worker', 'sync')},
                'db': {'image': 'sha256:db', 'environment': {'POSTGRES_DB': 'meme_arsenal', 'POSTGRES_USER': 'meme_arsenal',
                       'POSTGRES_PASSWORD_FILE': '/run/secrets/postgres_password'}, 'volumes': [
                       {'target': '/var/lib/postgresql/data', 'type': 'bind', 'source': str(target / 'state/postgres-data')},
                       {'target': '/run/secrets/postgres_password', 'type': 'bind', 'source': str(target / 'secrets/postgres_password')}]}}}
            # The real Compose file declares optional sync secrets even when
            # Telegram was never configured and those files do not exist.
            config['services']['sync']['environment'] = {
                **env, 'TELEGRAM_API_ID_FILE': '/run/secrets/telegram_api_id',
                'TELEGRAM_API_HASH_FILE': '/run/secrets/telegram_api_hash'}
            if failure == 'bind':
                config['services']['db']['volumes'][0]['source'] = '/old/DB'
            if failure == 'secret':
                config['services']['app']['environment'] = {
                    **env, 'GEMINI_API_KEY_FILE': '/run/secrets/missing-provider-key'}
            return json.dumps(config).encode()
        if command[:3] == ('docker', 'image', 'inspect'):
            return json.dumps([dict(Id=('sha256:wrong' if failure == 'image' else 'sha256:app') if command[3] == 'fixture:retained' else 'sha256:db', Os='linux', Architecture='amd64')]).encode()
        if 'ls' in command and failure == 'project':
            return b'existing-resource'
        if 'ps' in command:
            return b'new-db'
        if 'pg_restore' in command:
            assert kwargs['stdin'].read() == b'fixture'
            if '--list' in command:
                return b'fixture-list'
            assert {'--single-transaction', '--exit-on-error', '--no-owner', '--no-privileges'} <= set(command)
            if failure == 'restore':
                raise RuntimeError('restore failure')
            if failure == 'cancel':
                subject.cancelled = True
            subject.check()
        return b''
    subject.run = run
    subject.inspect = lambda **_: {'State': {'Health': {'Status': 'healthy'}}}
    def sql(query):
        events.append(('sql', query))
        if query == restore.EMPTY_SQL:
            return ['0']
        if 'server_version_num' in query:
            return ['160000']
        if 'version_num' in query:
            if failure == 'postcommit':
                raise RuntimeError('postcommit failure')
            return ['013_explicit_recipe_grants']
        if 'pg_extension' in query:
            return ['1']
        if 'anonymous_sessions' in query:
            # PostgreSQL json_agg(composite) adds a newline between records.
            return ['[{"id":"other-fixture"}, ', json.dumps({'id': 'Case Unicode Ω'}) + ']']
        return ['[]']
    subject.sql = sql
    subject.stop = lambda: events.append(('clean-stop',))
    if failure:
        expected = 'Matching-source schema head required' if failure == 'schema' else None
        with pytest.raises(RuntimeError, match=expected):
            subject.execute()
        assert subject.record['phase'] != 'complete'
    else:
        subject.execute()
        assert subject.record['phase'] == 'complete'
        assert events[-1] == ('clean-stop',)
    mutations = [c for c in events if 'up' in c]
    if failure in ('project', 'image', 'bind', 'secret'):
        assert mutations == []
        return
    assert len(mutations) == 1 and mutations[0][-1] == 'db'
    assert not any('alembic' in c or 'initialize' in c for c in events)
    empty = next(i for i,c in enumerate(events) if c == ('sql', restore.EMPTY_SQL))
    dump = next(i for i,c in enumerate(events) if 'pg_restore' in c)
    assert empty < dump
