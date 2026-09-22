"""Recognize long-lived chat streams for request latency accounting."""


def is_expected_stream_request(method: str, path: str) -> bool:
    if method != "GET" or not isinstance(path, str):
        return False
    return (
        path.startswith("/api/chat/resume/")
        or (path.startswith("/api/chat/work/") and path.endswith("/events/stream"))
        or (path.startswith("/api/chat/subagents/") and path.endswith("/events/stream"))
    )
