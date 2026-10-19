"""Identify a failed, safely retryable MCP startup; never accept its registry."""
from pathlib import Path

REVISION = 'bound-empty-mcp-startup-retry-v2'


def pending_init(event, *, session, model, workspace):
    return (isinstance(event, dict) and event.get('type') == 'system'
        and event.get('subtype') == 'init' and event.get('session_id') == session
        and event.get('model') == model and event.get('cwd') == str(Path(workspace))
        and event.get('tools') == []
        and event.get('mcp_servers') == [{'name': 'cowork', 'status': 'pending'}]
        and 'message' not in event)


def pending_proxy_result(event, *, session):
    """The pinned CLI can emit our local rejection while startup is stopping.

    Call only after a bound empty pending init with no tool activity. This
    permits another startup attempt; it never accepts an incomplete registry.
    """
    usage = event.get('usage') if isinstance(event, dict) else None
    return (isinstance(event, dict) and event.get('type') == 'result'
        and event.get('session_id') == session and event.get('is_error') is True
        and event.get('api_error_status') == 400
        and event.get('terminal_reason') == 'api_error'
        and event.get('result') == 'API Error: 400 Isolated proxy rejected request'
        and event.get('duration_api_ms') == 0
        and event.get('total_cost_usd') == 0 and event.get('modelUsage') == {}
        and event.get('permission_denials') == [] and isinstance(usage, dict)
        and all(usage.get(key) == 0 for key in (
            'input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')))


def retryable_startup_result(final):
    # Controller-owned structured reason, not native/model/tool prose matching.
    return (isinstance(final, dict) and final.get('is_error') is True
        and final.get('terminal_reason') == 'mcp_startup_pending'
        and final.get('error') == {'code': 'mcp_startup_pending',
            'error': 'Bound cowork MCP initialization remained pending; retry the same session.'})


def startup_error():
    return {'code': 'mcp_startup_pending',
            'error': 'Bound cowork MCP initialization remained pending; retry the same session.'}
