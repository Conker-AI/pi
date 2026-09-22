import httpx

from pi.toolgate import ToolGateClient, ToolResult, ToolPending, ApprovalRequired


def test_workflow_capability_pins_dispatch_and_checks_receipt(monkeypatch):
    digest = 'a' * 64
    alias = f'workflow:daily.brief:3:{digest}'
    calls = []
    responses = [
        {'code': 'CONFIRMATION_REQUIRED', 'request_id': 'request-1'},
        {'code': 'OK', 'status': 'completed', 'action_id': 'pi_exact',
         'publication': {'id': 'daily.brief', 'version': 3, 'digest': digest}, 'result': {'answer': 7}},
    ]
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json=responses.pop(0))
    monkeypatch.setattr(httpx, 'post', post)
    client = ToolGateClient('http://gate', 'execution-only')
    assert isinstance(client.invoke(alias, {}, action_id='pi_exact'), ApprovalRequired)
    result = client.invoke(alias, {}, 'request-1', action_id='pi_exact')
    assert result == ToolResult(True, {'answer': 7}, alias)
    for url, kwargs in calls:
        assert url == 'http://gate/v2/automations/daily.brief/run'
        assert kwargs['json']['published_version'] == 3
        assert kwargs['json']['expected_publication_digest'] == digest
        assert kwargs['json']['action_id'] == 'pi_exact'
        assert kwargs['headers'] == {'X-ToolGate-Execution-Key': 'execution-only'}
    assert calls[1][1]['json']['approval_request_id'] == 'request-1'
    wrong = httpx.Response(200, json={'code': 'OK', 'status': 'completed', 'action_id': 'pi_exact',
        'publication': {'id': 'daily.brief', 'version': 4, 'digest': digest}, 'result': {}})
    assert isinstance(client._outcome(wrong, alias, {}, 'pi_exact'), ToolPending)


def test_workflow_catalogue_joins_tools_without_leaking_execution_metadata(monkeypatch):
    alias = 'workflow:daily.brief:3:' + 'a' * 64
    def get(url, **kwargs):
        rows = [{'id': alias, 'name': 'Daily brief', 'description': 'Published workflow', 'inputs': []}] if url.endswith('published-workflows') else [{'id': 'echo', 'name': 'Echo'}]
        return httpx.Response(200, json=rows, request=httpx.Request('GET', url))
    monkeypatch.setattr(httpx, 'get', get)
    assert [tool.id for tool in ToolGateClient('http://gate', 'execution-only').tools()] == ['echo', alias]
