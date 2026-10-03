from contextlib import closing
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from pi import browser_contract, session_settings, universal_search as search
from pi.store import Store


def test_real_source_adapters(tmp_path):
    from pi import projects, artifacts, agents, tasks
    with closing(Store(tmp_path / 'adapters.db')) as store:
        sid = store.create_session(title='Planning')
        project = projects.create(store, projects.Fields(name='Needle project', description='Needle description', instructions='Needle instructions'))
        artifact = artifacts.create(store, artifacts.Create(title='Needle artifact', content=artifacts.Text(kind='markdown', text='Needle retained text')))
        agent = agents.create(store, agents.AgentInput(name='Needle agent', role='Needle role', instructions='Needle instructions', modelId=None, toolIds=[], memory=agents.MemorySelection(scope='none', memoryIds=[])))
        task = tasks.create(store, tasks.CreateTask(request_id='search_adapter_request', session_id=sid, outcome='Needle outcome', criteria=['Needle criterion']))
        cfg = {'sources': ['projects', 'artifacts', 'agents', 'tasks']}
        for stage in ('metadata', 'text'):
            rows, coverage = search.lexical(store, SimpleNamespace(client=None), None, 'needle', cfg, stage)
            assert {row['recordId'] for row in rows} == {project['id'], artifact['id'], agent['id'], task['id']}
            assert all(row['status'] == ('partial' if stage == 'text' and row['source'] == 'artifacts' else 'searched') for row in coverage)


def client_for(store, memory=None, authorize=lambda: None):
    app = FastAPI()
    app.include_router(search.router(lambda: store, lambda: SimpleNamespace(client=memory), lambda: None, lambda: {}, authorize))
    return TestClient(app)


def test_owner_boundary():
    for path in ('/search', '/search/settings', '/search/capabilities'):
        assert browser_contract.owner_allowed('GET', path)
        assert not browser_contract.runtime_allowed('GET', path)
    assert browser_contract.owner_allowed('POST', '/search/settings')
    assert not browser_contract.session_only_write('POST', '/search/settings')


def test_settings_revision_persistence_and_unsupported_stages(tmp_path):
    path = tmp_path / 'search.db'
    with closing(Store(path)) as store:
        client = client_for(store)
        initial = client.get('/search/settings').json()
        assert not initial['configuration']['semantic']
        assert not initial['configuration']['reranking']
        configuration = {**initial['configuration'], 'sources': ['conversations'], 'exactText': False}
        payload = {'configuration': configuration, 'expected_revision': 0}
        assert client.post('/search/settings', json=payload).json()['revision'] == 1
        assert client.post('/search/settings', json=payload).status_code == 409
        for key in ('semantic', 'reranking'):
            assert client.post('/search/settings', json={'configuration': {**configuration, key: True}, 'expected_revision': 1}).status_code == 422
        assert client.get('/search', params={'q': 'x', 'stage': 'text'}).json()['results'] == []
    with closing(Store(path)) as store:
        assert search.load_settings(store)['configuration'] == configuration


def test_exact_unicode_pagination_private_and_forgotten(tmp_path):
    from pi import forgetting
    path = tmp_path / 'search.db'
    with closing(Store(path)) as store:
        sid = store.create_session(title='Planning')
        ids = [store.append_message(sid, 'user' if i % 2 else 'assistant', 'שלום needle')['id'] for i in range(65)]
        private = store.create_session(title='Secret')
        session_settings.save(store, private, session_settings.Update(expected_revision=0, settings=session_settings.Settings(agentId='companion', privacy=session_settings.Privacy(memoryDisabled=True, harnessDisabled=False))))
        store.append_message(private, 'user', 'שלום needle')
        client = client_for(store)
        params = {'q': 'שלום', 'stage': 'text', 'limit': 30}
        results = []
        while True:
            page = client.get('/search', params=params).json()
            results.extend(page['results'])
            if not page['nextCursor']:
                break
            params['cursor'] = page['nextCursor']
        assert {row['recordId'] for row in results} == set(ids)
        assert all('message=' in row['href'] and row['matchType'] == 'text' for row in results)
        assert client.get('/search', params={**params, 'q': 'changed', 'cursor': 'bad:30'}).status_code == 422
        assert client.get('/search', params={'q': '   '}).status_code == 422
    forgetting.forget(path, sid, forgetting.preview(path, sid)['confirmation'])
    with closing(Store(path)) as store:
        assert client_for(store).get('/search', params={'q': 'needle', 'stage': 'text'}).json()['results'] == []


def test_disabled_semantics_make_no_calls_and_failure_retains_literal_results(tmp_path):
    class Memory:
        def retrieve(self, query):
            raise AssertionError('disabled stage made a provider call')

        def inspect(self, *args):
            raise RuntimeError('unavailable')

    with closing(Store(tmp_path / 'search.db')) as store:
        sid = store.create_session(title='Needle')
        store.append_message(sid, 'user', 'literal needle')
        client = client_for(store, Memory())
        assert client.get('/search', params={'q': 'needle', 'stage': 'semantic'}).json()['results'] == []
        page = client.get('/search', params={'q': 'needle', 'stage': 'text'}).json()
        assert len(page['results']) == 1
        assert {'source': 'memory', 'status': 'unavailable'} in page['coverage']
        assert page['ranking']['status'] == 'disabled'


def test_authentication_is_required(tmp_path):
    def deny():
        raise HTTPException(401)
    with closing(Store(tmp_path / 'search.db')) as store:
        assert client_for(store, authorize=deny).get('/search', params={'q': 'needle'}).status_code == 401


def test_literal_projection_and_cursor_invalidation(tmp_path):
    with closing(Store(tmp_path / 'search.db')) as store:
        assert search.record('tools', 'x', 'title', '100%_ literal', '/tools', '%_', 'text')['matchType'] == 'text'
        assert search.record('tools', 'x', 'needle', 'unrelated body', '/tools', 'needle', 'text') is None
        client = client_for(store)
        sid = store.create_session(title='Needle')
        for _ in range(3):
            store.append_message(sid, 'user', 'needle')
        cursor = client.get('/search', params={'q': 'needle', 'stage': 'text', 'limit': 1}).json()['nextCursor']
        cfg = search.load_settings(store)['configuration']
        client.post('/search/settings', json={'configuration': cfg, 'expected_revision': 0})
        assert client.get('/search', params={'q': 'needle', 'stage': 'text', 'cursor': cursor}).status_code == 422


def test_semantic_fallback_is_labeled_literal_and_source_failure_is_reported(tmp_path):
    class Memory:
        def retrieve(self, query):
            return {'retrieval': {'mode': 'lexical'}, 'memories': [{'id': 'mem_one', 'summary': 'Needle'}]}
    with closing(Store(tmp_path / 'search.db')) as store:
        cfg = search.Settings(sources=['memory'], semantic=True)
        search.save_settings(store, search.SaveSettings(expected_revision=0, configuration=cfg), semantic_available=True, ranking_available=False)
        page = client_for(store, Memory()).get('/search', params={'q': 'needle', 'stage': 'semantic'}).json()
        assert page['results'][0]['matchType'] == 'text'
        assert page['coverage'] == [{'source': 'memory', 'status': 'degraded'}]
        unavailable = client_for(store).get('/search', params={'q': 'needle', 'stage': 'semantic'}).json()
        assert unavailable['coverage'] == [{'source': 'memory', 'status': 'unavailable'}]


def test_ranking_is_bounded_validated_and_keeps_literals_on_failure(tmp_path, monkeypatch):
    from pi.providers import Completion, ProviderUnavailable
    records = [search.record('tools', str(i), 'Needle', f'needle {i}', '/tools', 'needle', 'text') for i in range(12)]
    monkeypatch.setattr(search, 'ranking_configuration', lambda store: {'enabled': True})
    def dispatch(cfg, role, messages, providers):
        import json
        assert role == 'search-ranking'
        previews = json.loads(messages[-1].content)['search_previews']
        assert len(previews) == 8 and max(map(len, previews.values())) <= 160
        return {'modelId': 'ranking', 'completion': Completion(text=json.dumps({'order': list(reversed(previews))}), model='ranking', provider='test')}
    monkeypatch.setattr(search.model_roles, 'dispatch', dispatch)
    with closing(Store(tmp_path / 'search.db')) as store:
        ranked, receipt = search.rerank(store, {}, 'needle', records)
        assert ranked[:8] == list(reversed(records[:8])) and ranked[8:] == records[8:]
        assert receipt['status'] == 'ranked' and all(item['matchType'] == 'text' for item in ranked)
        def fail(*args):
            raise ProviderUnavailable('private error')
        monkeypatch.setattr(search.model_roles, 'dispatch', fail)
        assert search.rerank(store, {}, 'needle', records) == (records, {'status': 'fallback'})
