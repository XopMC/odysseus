from types import SimpleNamespace

import pytest

from app import _SlowRequestLogMiddleware


def _request(path: str, method: str = "GET"):
    return SimpleNamespace(method=method, url=SimpleNamespace(path=path))


async def _no_response(_request):
    raise RuntimeError("No response returned.")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/chat/resume/session-id",
        "/api/chat/work/session-id/events/stream",
        "/api/chat/subagents/session-id/events/stream",
    ],
)
async def test_expected_stream_disconnect_is_not_reported_as_server_error(path):
    middleware = _SlowRequestLogMiddleware(lambda *_args, **_kwargs: None)

    response = await middleware.dispatch(_request(path), _no_response)

    assert response.status_code == 499


@pytest.mark.asyncio
async def test_no_response_from_regular_handler_still_fails_loudly():
    middleware = _SlowRequestLogMiddleware(lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="No response returned"):
        await middleware.dispatch(_request("/api/health"), _no_response)
