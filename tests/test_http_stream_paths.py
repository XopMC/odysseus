from src.http_stream_paths import is_expected_stream_request


def test_only_known_read_only_chat_streams_are_long_lived():
    assert is_expected_stream_request("GET", "/api/chat/work/s/events/stream")
    assert is_expected_stream_request("GET", "/api/chat/subagents/s/events/stream")
    assert is_expected_stream_request("GET", "/api/chat/resume/s")
    assert not is_expected_stream_request("POST", "/api/chat/work/s/events/stream")
    assert not is_expected_stream_request("GET", "/api/chat/work/s/goal")
    assert not is_expected_stream_request("GET", "/api/team/events/stream")
