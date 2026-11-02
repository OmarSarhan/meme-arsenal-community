"""Sequential development-schema rehearsal, never an old application runtime."""
import hashlib
import base64
import json
import shutil
import subprocess

BASELINE = '012_public_share_codes'
# This historical rehearsal stays fixed as newer revisions are added.
HEAD = '013_explicit_recipe_grants'
PG_OPTIONS = '-c statement_timeout=30000 -c lock_timeout=10000'

SEED = """
INSERT INTO anonymous_sessions(id,created_at,last_seen_at)
VALUES ('fixture-owner',now(),now());
INSERT INTO telegram_messages(id,telegram_message_id,chat_id,import_status,created_at,updated_at,raw_metadata_json)
VALUES (1,1,'fixture','imported',now(),now(),'{"fixture":"Ω"}');
INSERT INTO media_items(id,telegram_message_id,media_type,original_path,processing_status,created_at,updated_at)
VALUES (1,1,'image','data/fixture.png','ready',now(),now()),
       (2,1,'image','data/fixture.png','ready',now(),now()),
       (3,1,'image','data/fixture.png','ready',now(),now());
INSERT INTO remix_jobs(id,session_id,left_id,right_id,safety_mode,tweaks_json,status,message,result_json,saved_media_item_id,created_at,updated_at)
VALUES ('fixture-job','fixture-owner',1,2,'standard','{}','saved','Saved','{}',3,now(),now());
INSERT INTO remix_attempts(job_id,attempt,mitigation_id,label,description,status,message,created_at)
VALUES ('fixture-job',1,'original','fixture','','succeeded','Generated',now());
INSERT INTO remix_sources(job_id,remix_media_item_id,source_media_item_id,position,created_at)
VALUES ('fixture-job',3,1,0,now()),('fixture-job',3,2,1,now());
INSERT INTO anonymous_session_curation(session_id,media_item_id,favorite,archived,manual_tags_json,notes,updated_at)
VALUES ('fixture-owner',1,true,false,'["Ω"]','Checkpoint notes',now());
INSERT INTO curation(media_item_id,favorite,archived,manual_tags_json,notes,updated_at)
VALUES (2,false,true,'[]','Global fixture',now());
INSERT INTO classifications(media_item_id,model,prompt_version,summary,visible_text,created_at)
VALUES (1,'mock','oma30','Preserved Ω classification','fixture',now());
INSERT INTO public_media_shares(id,media_item_id,token_id,active,created_at,updated_at)
VALUES (1,1,'legacy-active',true,now(),now()),(2,2,'legacy-inactive',false,now(),now());
INSERT INTO public_share_codes(code,token_id,card,created_at)
VALUES ('ABCDE','legacy-active',false,now()),('FGHIJ','legacy-active',true,now()),
       ('KLMNO','legacy-inactive',false,now());
"""


def rehearse(run, root, recipe, network, image, hash_file):
    """Use fresh standard-coordinate instances sequentially, exact retained images."""
    checkpoint = root / 'schema-checkpoint'
    checkpoint.mkdir(mode=0o700)
    baseline_rows = baseline_catalog = None
    for suffix in ('failure', 'checkpoint-retry'):
        instance = root / ('schema-' + suffix)
        project = root.name.lower().replace('_', '-') + '-' + suffix
        run(['python3', str(recipe.parent / 'init.py'), str(instance), '--site-url',
             'https://fixture.example.invalid', '--image', image, '--auth-user', 'fixture',
             '--auth-hash-file', str(hash_file)], timeout=30)
        (instance / 'secrets/owner_session_id').write_text('fixture-owner')
        if suffix == 'checkpoint-retry':
            shutil.rmtree(instance / 'secrets')
            shutil.copytree(checkpoint / 'secrets', instance / 'secrets')
        compose = ['docker', 'compose', '-p', project, '--env-file', str(instance / 'config/compose.env'),
                   '-f', str(recipe), '-f', str(network)]
        migration_name = project + '-migration'

        def sql(query):
            return run([*compose, 'exec', '-T', '-e', 'PGCONNECT_TIMEOUT=10', '-e', 'PGOPTIONS=' + PG_OPTIONS,
                        'db', 'psql', '-X', '-v', 'ON_ERROR_STOP=1', '-U', 'meme_arsenal',
                        '-d', 'meme_arsenal', '-Atc', query], capture=True, timeout=45).stdout.strip()

        def migrate(revision, check=True):
            return run([*compose, 'run', '--rm', '--no-deps', '--name', migration_name, '-T',
                        '-e', 'ARSENAL_INSTALL_DASHBOARD=0', '-e', 'PGCONNECT_TIMEOUT=10',
                        '-e', 'PGOPTIONS=' + PG_OPTIONS, 'app', 'alembic', 'upgrade', revision],
                       capture=True, check=check, timeout=120)

        def rows():
            tables = sql("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename").splitlines()
            return {table: json.loads(sql(f"SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]') FROM {table} t"))
                    for table in tables}

        def catalog():
            return sql("SELECT jsonb_agg(to_jsonb(t) ORDER BY table_name,ordinal_position) FROM "
                       "(SELECT table_name,column_name,ordinal_position,data_type,is_nullable,column_default "
                       "FROM information_schema.columns WHERE table_schema='public') t")

        try:
            config = json.loads(run([*compose, 'config', '--format', 'json'], capture=True, timeout=30).stdout)
            assert all(n.get('internal') for n in config['networks'].values())
            assert all(not s.get('ports') for s in config['services'].values())
            run([*compose, 'up', '-d', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '120', 'db'], timeout=150)
            dbid = run([*compose, 'ps', '-q', 'db'], capture=True, timeout=15).stdout.strip()
            inspected = json.loads(run(['docker', 'inspect', dbid], capture=True, timeout=15).stdout)[0]
            assert not inspected['HostConfig']['PortBindings']
            assert int(sql('SHOW server_version_num')) // 10000 == 16
            if suffix == 'failure':
                migrate(BASELINE)
                sql(SEED)
                # Synthetic file, independent of all application initialization.
                data = instance / 'state/reserve/data'
                data.mkdir(exist_ok=True)
                (data / 'fixture.png').write_bytes(base64.b64decode(
                    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='))
                baseline_rows, baseline_catalog = rows(), catalog()
                assert baseline_rows['alembic_version'] == [{'version_num': BASELINE}]
                assert 'recipe_grant_json' not in baseline_catalog
                assert sql("SELECT extname FROM pg_extension WHERE extname='vector'") == 'vector'
                shutil.copytree(instance / 'state/reserve', checkpoint / 'reserve')
                shutil.copytree(instance / 'secrets', checkpoint / 'secrets')
                dump = checkpoint / 'database.dump'
                with dump.open('wb') as stream:
                    subprocess.run([*compose, 'exec', '-T', '-e', 'PGCONNECT_TIMEOUT=10', '-e',
                                    'PGOPTIONS=' + PG_OPTIONS, 'db', 'pg_dump', '-U', 'meme_arsenal',
                                    '-d', 'meme_arsenal', '-Fc'], stdout=stream, check=True,
                                   env={'PATH': '/usr/local/bin:/usr/bin:/bin'}, timeout=120)
                dump.chmod(0o600)
                sql("""CREATE FUNCTION oma30_fail() RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN RAISE EXCEPTION USING ERRCODE='P0030', MESSAGE='OMA30 fixture recall failure P0030'; END $$;
                    CREATE TRIGGER oma30_fail BEFORE UPDATE ON public_media_shares
                    FOR EACH STATEMENT EXECUTE FUNCTION oma30_fail();""")
                failed = migrate(HEAD, check=False)
                assert failed.returncode != 0
                error = failed.stdout + failed.stderr
                assert 'OMA30 fixture recall failure' in error and 'UPDATE public_media_shares' in error
                assert 'P0030' in error
                # Independent psql connections: transactional DDL and data rollback.
                assert rows() == baseline_rows and catalog() == baseline_catalog
                sql('DROP TRIGGER oma30_fail ON public_media_shares; DROP FUNCTION oma30_fail();')
                assert sql("SELECT count(*) FROM pg_proc WHERE proname='oma30_fail'") == '0'
            else:
                assert sql("SELECT count(*) FROM pg_tables WHERE schemaname='public'") == '0'
                with (checkpoint / 'database.dump').open('rb') as stream:
                    subprocess.run([*compose, 'exec', '-T', '-e', 'PGCONNECT_TIMEOUT=10', '-e',
                                    'PGOPTIONS=' + PG_OPTIONS, 'db', 'pg_restore', '-U', 'meme_arsenal',
                                    '-d', 'meme_arsenal', '--single-transaction', '--exit-on-error',
                                    '--no-owner', '--no-privileges'], stdin=stream, check=True,
                                   env={'PATH': '/usr/local/bin:/usr/bin:/bin'}, timeout=120)
                shutil.rmtree(instance / 'state/reserve')
                shutil.copytree(checkpoint / 'reserve', instance / 'state/reserve')
                shutil.rmtree(instance / 'secrets')
                shutil.copytree(checkpoint / 'secrets', instance / 'secrets')
                assert rows() == baseline_rows and catalog() == baseline_catalog
            migrate(HEAD)
            after = rows()
            assert after['alembic_version'] == [{'version_num': HEAD}]
            for table, original in baseline_rows.items():
                if table not in ('alembic_version', 'public_media_shares'):
                    assert after[table] == original, table
            for row in after['public_media_shares']:
                old = next(r for r in baseline_rows['public_media_shares'] if r['id'] == row['id'])
                assert row['active'] is False and row['recipe_grant_json'] is None
                assert row['revoked_at'] == row['updated_at'] and row['revoked_at'] is not None
                assert {k: v for k, v in row.items() if k not in ('active', 'recipe_grant_json', 'revoked_at', 'updated_at')} == {
                    k: v for k, v in old.items() if k not in ('active', 'revoked_at', 'updated_at')}
            migrate(HEAD)
            assert rows() == after
            for tree, target in (('reserve', instance / 'state/reserve'), ('secrets', instance / 'secrets')):
                for path in (checkpoint / tree).rglob('*'):
                    if path.is_file():
                        assert path.read_bytes() == (target / path.relative_to(checkpoint / tree)).read_bytes()
            evidence = {'baseline': BASELINE, 'candidate': HEAD, 'path': suffix,
                        'counts': {k: len(v) for k, v in baseline_rows.items()},
                        'catalog_sha256': hashlib.sha256(baseline_catalog.encode()).hexdigest()}
            (checkpoint / (suffix + '.json')).write_text(json.dumps(evidence))
            print('Development schema failure/recovery proof:', json.dumps(evidence))
        finally:
            # Killing a timed-out Docker CLI does not stop its one-shot container.
            # Remove only this fixture's exact labelled/mounted migration child.
            try:
                leftover = run(['docker', 'inspect', migration_name], capture=True, check=False, timeout=15)
                if leftover.returncode == 0:
                    child = json.loads(leftover.stdout)[0]
                    labels = child['Config']['Labels']
                    assert labels['com.docker.compose.project'] == project
                    assert labels['com.docker.compose.service'] == 'app'
                    assert any(m['Destination'] == '/reserve' and m['Source'] == str(instance / 'state/reserve')
                               for m in child['Mounts'])
                    run(['docker', 'rm', '-f', child['Id']], timeout=30)
            finally:
                cleanup = run([*compose, 'down', '--remove-orphans'], check=False, timeout=60)
                if cleanup.returncode:
                    raise RuntimeError(f'Could not clean schema fixture {project}')
