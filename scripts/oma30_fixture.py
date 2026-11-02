"""Fixed community restore fixture; executed only in disposable CI containers."""

ENRICH = r'''
import hashlib
import json
from pathlib import Path
from meme_arsenal.config import load_settings
from meme_arsenal.db import session_scope
from meme_arsenal.models import RemixJobRecord, RemixAttemptRecord
settings = load_settings()
state_path = Path('/reserve/.community-smoke.json')
state = json.loads(state_path.read_text())
with session_scope(settings) as db:
    saved = db.get(RemixJobRecord, state['job'])
    assert saved.status == 'saved' and saved.saved_media_item_id
    state['remix_id'] = saved.saved_media_item_id
    draft = settings.remix_drafts_dir / saved.result_json['result']['image_path']
    state['ready_digest'] = hashlib.sha256(draft.read_bytes()).hexdigest()
    db.add(RemixJobRecord(id='community-restored-ready', session_id=saved.session_id,
        left_id=saved.left_id, right_id=saved.right_id, status='ready', message='Ready',
        result_json=saved.result_json, safety_mode=saved.safety_mode))
    db.add(RemixJobRecord(id='community-restored-queued', session_id=saved.session_id,
        left_id=saved.left_id, right_id=saved.right_id, status='queued', message='Queued',
        safety_mode=saved.safety_mode))
    db.add(RemixAttemptRecord(job_id=saved.id, attempt=1, status='succeeded', message='Fixture ready'))
    db.add(RemixAttemptRecord(job_id='community-restored-ready', attempt=1, status='succeeded', message='Fixture ready'))
state_path.write_text(json.dumps(state))
state_path.chmod(0o600)
'''

SHARES = r'''
from base64 import b64encode
from contextlib import closing
from http.client import HTTPSConnection
from pathlib import Path
import json, ssl, sys
from urllib.parse import urlsplit
state_path = Path('/reserve/.community-smoke.json')
state = json.loads(state_path.read_text())
auth = {'Authorization': 'Basic ' + b64encode(b'fixture:community-fixture-password').decode()}
context = ssl.create_default_context(cafile='/tmp/community-ca.crt')
def request(host, path, method='GET', headers=None, body=None):
    with closing(HTTPSConnection(host, context=context, timeout=30)) as client:
        client.request(method, path, body=body, headers=headers or {})
        response = client.getresponse()
        return response.status, response.read()
private, public = 'fixture.example.invalid', 'share.fixture.example.invalid'
endpoint = f"/api/items/{state['saved_id']}/share"
recipe = f"/api/items/{state['remix_id']}/share?card=true"
if sys.argv[1] == 'prepare':
    curation = {'favorite': True, 'notes': 'Restore Ω owner notes', 'manual_tags': ['restore', 'Ω']}
    status, body = request(private, f"/api/session/items/{state['saved_id']}", 'PATCH',
                           {**auth, 'Content-Type': 'application/json'}, json.dumps(curation))
    assert status == 200
    item = json.loads(body)
    assert all(item[key] == value for key, value in curation.items())
    def publish(path):
        status, body = request(private, path, 'POST', auth)
        assert status == 200
        return json.loads(body)
    old = publish(endpoint)
    assert request(private, endpoint, 'DELETE', auth)[0] == 200
    state['recalled'] = old['url']
    state['output'] = publish(endpoint)
    state['recipe'] = publish(recipe)
    state_path.write_text(json.dumps(state))
    state_path.chmod(0o600)
else:
    output = urlsplit(state['output']['url']).path
    card = urlsplit(state['recipe']['card_url']).path
    assert request(public, output)[0] == 200
    assert request(public, output + '/card')[0] == 404
    assert request(public, card)[0] == 200
    assert request(public, card + '/card/image?source=A')[0] == 404
    assert request(public, card + '/card/image?source=B')[0] == 200
    assert request(public, urlsplit(state['recalled']).path)[0] == 404
    assert request(private, endpoint, 'DELETE', auth)[0] == 200
    status, body = request(private, endpoint, 'POST', auth)
    assert status == 200 and json.loads(body)['url'] != state['output']['url']
    assert request(public, output)[0] == 404
    assert request(public, urlsplit(state['recalled']).path)[0] == 404
print('Retained restore capabilities:', sys.argv[1])
'''

WORKER = r'''
from dataclasses import replace
from pathlib import Path
import hashlib, json, socket
from PIL import Image
from sqlalchemy import select
from meme_arsenal.config import load_settings
from meme_arsenal.db import session_scope
from meme_arsenal.models import RemixJobRecord, RemixAttemptRecord
from meme_arsenal import online_worker
from meme_arsenal.remix_execution import capture_execution
settings = replace(load_settings(), ai_provider='google', google_api_key='fixture-only',
                   online_embeddings_enabled=False)
calls = []
expected_result = {}
expected_digest = None
connect = socket.socket.connect
allowed = {(socket.gethostbyname('db'), 5432), (socket.gethostbyname('redis'), 6379)}
def guarded(sock, address):
    if isinstance(address, tuple) and address[:2] in allowed:
        return connect(sock, address)
    calls.append('UNEXPECTED outbound')
    raise AssertionError('Unexpected fixture outbound')
socket.socket.connect = guarded
def forbidden(*args, **kwargs):
    calls.append('UNEXPECTED outbound')
    raise AssertionError('Unexpected fixture outbound')
socket.socket.connect_ex = forbidden
def generate(db, settings, left, right, **kwargs):
    global expected_digest
    from types import SimpleNamespace
    calls.append(kwargs['job_id'])
    assert kwargs['job_id'] == 'community-restored-queued'
    path = settings.remix_drafts_dir / (kwargs['job_id'] + '.png')
    Image.new('RGB', (96, 96), 'purple').save(path)
    expected_result.update(image_path=path.name, summary='Restored worker result')
    expected_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return SimpleNamespace(payload=lambda: dict(expected_result))
online_worker.generate_remix = generate
with session_scope(settings) as db:
    before = {j.id: (j.status, j.result_json, j.saved_media_item_id, j.updated_at)
              for j in db.scalars(select(RemixJobRecord)) if j.status in ('ready', 'saved')}
online_worker.run_worker(settings, once=True)
assert calls == ['community-restored-queued'], calls
with session_scope(settings) as db:
    job = db.get(RemixJobRecord, 'community-restored-queued')
    assert job.status == 'ready'
    assert job.execution_capture_json == capture_execution(settings, 'legacy_first_execution').payload()
    assert job.execution_first_captured_attempt == 1
    assert job.result_json['result'] == expected_result
    result_path = settings.remix_drafts_dir / job.result_json['result']['image_path']
    assert hashlib.sha256(result_path.read_bytes()).hexdigest() == expected_digest
    attempts = list(db.scalars(select(RemixAttemptRecord).where(RemixAttemptRecord.job_id == job.id)))
    assert len(attempts) == 1 and attempts[0].status == 'succeeded'
    assert before == {j.id: (j.status, j.result_json, j.saved_media_item_id, j.updated_at)
                      for j in db.scalars(select(RemixJobRecord)) if j.id in before}
evidence = Path('/reserve/.oma30-worker.json')
evidence.write_text(json.dumps({'calls': calls, 'image_sha256': expected_digest}))
evidence.chmod(0o600)
print('Restored SQL fallback queue claimed exact job; ready/saved unchanged')
'''

VERIFY = r'''
from meme_arsenal.models import AnonymousSessionCuration, RemixSourceRecord
for headers in (AUTH, owner):
    item = json.loads(request(f"/api/items/{state['saved_id']}", headers=headers)[0])
    assert item['summary'] == 'Community fixture edited'
    assert item['favorite'] is True and item['notes'] == 'Restore Ω owner notes'
    assert json.loads(request('/api/remix/jobs/community-restored-ready', headers=headers)[0])['status'] == 'ready'
    image = request('/api/remix/jobs/community-restored-ready/image', headers=headers)[0]
    assert hashlib.sha256(image).hexdigest() == state['ready_digest']
    assert Image.open(BytesIO(image)).size == (96, 96)
    replay = json.loads(request(f"/api/remix/jobs/{state['job']}/save", 'POST', b'', headers)[0])
    assert replay['item']['id'] == state['remix_id']
with session_scope(settings) as db:
    links = list(db.scalars(select(RemixSourceRecord).where(RemixSourceRecord.job_id == state['job'])
                           .order_by(RemixSourceRecord.position)))
    assert [(link.source_media_item_id, link.position) for link in links] == [(state['id'], 0), (state['saved_id'], 1)]
    assert all(link.remix_media_item_id == state['remix_id'] for link in links)
print('Restored cookie/no-cookie owner curation, ready result and saved replay/ordered lineage passed')
'''

WORKER_VERIFY = r'''
evidence = json.loads(Path('/reserve/.oma30-worker.json').read_text())
assert evidence['calls'] == ['community-restored-queued']
for headers in (AUTH, owner):
    image = request('/api/remix/jobs/community-restored-queued/image', headers=headers)[0]
    assert hashlib.sha256(image).hexdigest() == evidence['image_sha256']
    assert Image.open(BytesIO(image)).size == (96, 96)
print('Restored worker result delivered exact generated image with and without cookie')
'''
