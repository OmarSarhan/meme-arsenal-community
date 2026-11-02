from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json

import pytest

from meme_arsenal import google_api as api
from meme_arsenal.ai_circuit import (
    acquire_ai_credit_probe, complete_ai_credit_probe, open_ai_credit_circuit,
)


@pytest.fixture(autouse=True)
def reset_latch():
    api._LOCAL_CREDIT_CIRCUIT_UNPERSISTED.clear()
    yield
    api._LOCAL_CREDIT_CIRCUIT_UNPERSISTED.clear()


@pytest.fixture
def google(settings):
    return replace(settings, ai_provider='google', google_api_key='fake-key', ai_max_retries=5)


def transport(monkeypatch, response, during=None):
    requests = []

    def send(request, timeout):
        requests.append((request.full_url, json.loads(request.data)))
        if during:
            during()
        return BytesIO(json.dumps(response).encode())

    monkeypatch.setattr('urllib.request.urlopen', send)
    return requests


@pytest.mark.parametrize('operation,response', [
    ('classification', {'candidates': [{'finishReason': 'MAX_TOKENS'}]}),
    ('classification', {'candidates': [{'content': {'parts': [{'text': 'yes'}]}}]}),
    ('embedding', {'embedding': {'values': [0.2, 1]}}),
    ('remix', {'candidates': [{'content': {'parts': [{'text': '{"reason":"No image output"}'}]}}]}),
])
def test_exact_requests(monkeypatch, google, operation, response):
    open_ai_credit_circuit(google)
    calls = transport(monkeypatch, response)
    result = api.probe_google_credit(google, operation=operation, force=True)
    assert result.recovered and result.operation == operation
    assert len(calls) == 1
    url, body = calls[0]
    if operation == 'embedding':
        assert url.endswith('/models/mock-embedding:embedContent')
        assert body == {'model': 'models/mock-embedding', 'content': {'parts': [{'text': 'Credit probe.'}]},
                        'outputDimensionality': 32}
    elif operation == 'classification':
        assert url.endswith('/models/mock-classifier:generateContent')
        assert body == {'contents': [{'role': 'user', 'parts': [{'text': 'Reply with OK.'}]}],
                        'generationConfig': {'temperature': 0, 'maxOutputTokens': 1}}
    else:
        assert url.endswith('/models/' + google.remix_model + ':generateContent')
        assert set(body['generationConfig']) == {'responseFormat'}
        assert set(body['contents'][0]['parts'][0]) == {'text'}
        assert body['generationConfig']['responseFormat']['text']['schema']['required'] == ['reason']


@pytest.mark.parametrize('changes,explicit,expected', [
    ({}, None, 'classification'),
    ({'ai_provider': 'mock', 'embedding_provider': 'google', 'remix_provider': 'google',
      'online_embeddings_enabled': True}, None, 'embedding'),
    ({'classification_provider': 'mock', 'online_embeddings_enabled': False}, None, 'remix'),
    ({'classification_provider': 'mock', 'remix_provider': 'mock',
      'online_embeddings_enabled': False}, 'embedding', 'embedding'),
    ({'ai_provider': 'mock'}, None, None),
    ({'classification_provider': 'mock'}, 'classification', None),
    ({}, 'unknown', None),
])
def test_selection(monkeypatch, google, changes, explicit, expected):
    config = replace(google, **changes)
    open_ai_credit_circuit(config)
    calls = transport(monkeypatch, {'embedding': {'values': [1]}, 'candidates': [{'finishReason': 'MAX_TOKENS'}]})
    result = api.probe_google_credit(config, operation=explicit, force=True)
    assert result.request_made == bool(expected)
    assert len(calls) == bool(expected)
    if expected:
        assert result.operation == expected


@pytest.mark.parametrize('changes', [{'google_api_key': ''}, {'google_api_key': '  '}, {'ai_model': ' '}])
def test_preflight(monkeypatch, google, changes):
    calls = transport(monkeypatch, {})
    result = api.probe_google_credit(replace(google, **changes), force=True)
    assert result.outcome == 'unavailable' and not result.request_made
    assert not calls


@pytest.mark.parametrize('operation,response', [
    ('classification', {}), ('classification', {'error': {'message': 'denied'}}),
    ('classification', {'candidates': [{}]}), ('classification', {'candidates': [None]}),
    ('classification', {'promptFeedback': {'blockReason': 'SAFETY'}}),
    ('classification', {'candidates': [{'finishReason': 'SAFETY'}]}),
    *[('embedding', {'embedding': {'values': value}}) for value in
      ([], [True], ['1'], [float('nan')], [float('inf')])],
    *[('remix', {'candidates': [{'content': {'parts': [{'text': value}]}}]}) for value in
      ('fallback reason', '{}', '{"reason":""}', '{"reason":1}', '[]')],
])
def test_inconclusive_preserves_latch(monkeypatch, google, operation, response):
    open_ai_credit_circuit(google)
    api._LOCAL_CREDIT_CIRCUIT_UNPERSISTED.set()
    calls = transport(monkeypatch, response)
    result = api.probe_google_credit(google, operation=operation, force=True)
    assert result.outcome == 'failed' and not result.recovered and result.circuit.is_open
    assert len(calls) == 1 and api._LOCAL_CREDIT_CIRCUIT_UNPERSISTED.is_set()


@pytest.mark.parametrize('replacement_closes', [False, True])
def test_stale_actual_probe(monkeypatch, google, replacement_closes):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    open_ai_credit_circuit(google, now=now)
    api._LOCAL_CREDIT_CIRCUIT_UNPERSISTED.set()

    def replace_lease():
        second = acquire_ai_credit_probe(google, force=True, now=now + timedelta(days=1))
        assert second.acquired
        if replacement_closes:
            assert complete_ai_credit_probe(google, second.token, recovered=True).applied

    calls = transport(monkeypatch, {'candidates': [{'finishReason': 'MAX_TOKENS'}]}, replace_lease)
    result = api.probe_google_credit(google, force=True, now=now)
    assert result.outcome == 'stale' and not result.recovered
    assert result.circuit.is_open != replacement_closes
    assert api._LOCAL_CREDIT_CIRCUIT_UNPERSISTED.is_set() and len(calls) == 1


def test_expired_unreplaced_completion(google):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    open_ai_credit_circuit(google, now=now)
    lease = acquire_ai_credit_probe(google, force=True, now=now)
    assert not complete_ai_credit_probe(google, 'wrong', recovered=True).applied
    result = complete_ai_credit_probe(google, lease.token, recovered=True, now=now + timedelta(days=1))
    assert result.applied and not result.status.is_open


@pytest.mark.parametrize('boundary,made', [('acquire_ai_credit_probe', False), ('complete_ai_credit_probe', True)])
def test_persistence_uncertainty(monkeypatch, google, boundary, made):
    open_ai_credit_circuit(google)
    api._LOCAL_CREDIT_CIRCUIT_UNPERSISTED.set()
    calls = transport(monkeypatch, {'candidates': [{'finishReason': 'MAX_TOKENS'}]})

    def fail(*args, **kwargs):
        raise RuntimeError('synthetic DB failure')

    monkeypatch.setattr(api, boundary, fail)
    result = api.probe_google_credit(google, force=True)
    assert result.outcome == 'unavailable' and not result.recovered
    assert result.request_made == made and len(calls) == made
    assert api._LOCAL_CREDIT_CIRCUIT_UNPERSISTED.is_set()


def test_read_denial_does_not_observe_depletion(monkeypatch, google):
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic DB failure')

    monkeypatch.setattr(api, 'ai_circuit_status', fail)
    with pytest.raises(api.GeminiCreditCircuitOpenError):
        api.post_google_model(google, 'model', 'generateContent', {})
    assert not api._LOCAL_CREDIT_CIRCUIT_UNPERSISTED.is_set()


def test_observed_depletion_retries_persistence(monkeypatch, google):
    real_open = api.open_ai_credit_circuit

    def fail(*args, **kwargs):
        raise RuntimeError('synthetic DB failure')

    monkeypatch.setattr(api, 'open_ai_credit_circuit', fail)
    api._open_credit_circuit(google)
    assert api._LOCAL_CREDIT_CIRCUIT_UNPERSISTED.is_set()
    with pytest.raises(api.GeminiCreditCircuitOpenError):
        api._assert_credit_circuit_closed(google)
    assert api._LOCAL_CREDIT_CIRCUIT_UNPERSISTED.is_set()
    monkeypatch.setattr(api, 'open_ai_credit_circuit', real_open)
    with pytest.raises(api.GeminiCreditCircuitOpenError):
        api._assert_credit_circuit_closed(google)
    assert not api._LOCAL_CREDIT_CIRCUIT_UNPERSISTED.is_set()
    assert api.ai_circuit_status(google).is_open


def test_probe_transient_is_single_zero_retry_request(monkeypatch, google):
    from urllib.error import HTTPError

    open_ai_credit_circuit(google)
    calls = []

    def send(request, timeout):
        calls.append(request.full_url)
        raise HTTPError(request.full_url, 503, 'unavailable', {}, BytesIO(b'{}'))

    monkeypatch.setattr('urllib.request.urlopen', send)
    result = api.probe_google_credit(google, force=True)
    assert result.outcome == 'failed' and result.request_made and not result.recovered
    assert len(calls) == 1


def test_canonical_embedding_model(monkeypatch, google):
    google = replace(google, embedding_model='models/custom-embedding')
    open_ai_credit_circuit(google)
    calls = transport(monkeypatch, {'embedding': {'values': [1]}})
    assert api.probe_google_credit(google, operation='embedding', force=True).recovered
    assert calls[0][0].endswith('/models/custom-embedding:embedContent')
    assert calls[0][1]['model'] == 'models/custom-embedding'


@pytest.mark.parametrize('response,valid', [
    ({'candidates': [{'finishReason': 'MAX_TOKENS'}]}, True),
    ({'candidates': [{'content': {'parts': [{'text': 'OK'}]}}]}, True),
    ({'candidates': [{'finishReason': 'MAX_TOKENS', 'content': {'parts': [{'text': 1}]}}]}, False),
    ({'candidates': [{'finishReason': 'MAX_TOKENS'}, None]}, False),
])
def test_candidate_structure(response, valid):
    assert api._credit_probe_response_valid('classification', response) == valid
