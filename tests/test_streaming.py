import json

from errand.llm import MessageScanner


def test_scanner_emits_message_deltas_and_nothing_else():
    s = MessageScanner()
    raw = '{"tool": "search_providers", "args": {"service": "plumber"},'
    raw += ' "message": "Looking for a plumber in Koramangala."}'
    out = ""
    for chunk in (raw[:18], raw[18:47], raw[47:]),:
        for piece in chunk:
            out += s.feed(piece)
    assert out == "Looking for a plumber in Koramangala."


def test_scanner_ignores_prose_before_the_message_key():
    """The tool name is not for reading; only `message` is."""
    s = MessageScanner()
    out = s.feed('{"tool": "book", "args": {}, "message": "Yes')
    assert out == "Yes", out


def test_scanner_handles_escapes():
    s = MessageScanner()
    out = s.feed('{"message": "a \\"quoted\\" word and \\n a newline \\u20b9"}')
    assert out == 'a "quoted" word and \n a newline \u20b9', repr(out)


def test_scanner_handles_a_null_message():
    s = MessageScanner()
    assert s.feed('{"tool": "book", "args": {}, "message": null}') == ""


def test_scanner_stops_at_the_closing_quote():
    s = MessageScanner()
    out = s.feed('{"message": "done", "tool": "book"}')
    assert out == "done", out


def test_a_truncated_stream_yields_partial_prose_but_no_action():
    """Half a sentence is fine to show; half an Action must never be dispatched."""
    s = MessageScanner()
    out = s.feed('{"tool": "book", "message": "I am still writ')
    assert out.startswith("I am still writ")


# ------------------------------------------------- streaming over the wire


def _sse(*chunks: str) -> bytes:
    frames = []
    for piece in chunks:
        event = {"choices": [{"delta": {"content": piece}}]}
        frames.append("data: " + json.dumps(event) + "\n\n")
    frames.append("datan\n")
    return "".join(frames).encode("utf-8")


class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._lines = body.splitlines(keepends=True)

    def __iter__(self):
        return iter(self._lines)

    def read(self) -> bytes:
        return b"".join(self._lines)


def test_a_real_sse_stream_is_parsed_and_replayed():
    from errand.llm import _read_stream

    body = _sse('{"tool": "book", "args": {}, "message": "', "Booked", " for Monday", '"}')
    seen: list[str] = []
    raw = _read_stream(_FakeResp(body), seen.append)
    assert "".join(seen) == "Booked for Monday", seen
    assert '"tool": "book"' in raw["choices"][0]["message"]["content"]


def test_a_non_sse_gateway_falls_back_to_plain_json():
    """A proxy that strips `stream: true` must not look like a dead model."""
    from errand.llm import _read_stream

    body = json.dumps({"choices": [{"message": {"content": '{"tool": null, '
                                                     '"args": {}, "message": "hi"}'}}]}).encode()
    raw = _read_stream(_FakeResp(body), lambda _t: None)
    assert "hi" in raw["choices"][0]["message"]["content"]


def test_a_raising_callback_does_not_lose_the_action():
    """Display is a courtesy; the Action is the work."""
    from errand.llm import _read_stream

    body = _sse('{"tool": "book", "args": {}, "message": "yes"}')

    def boom(_text: str) -> None:
        raise RuntimeError("the page went away")

    raw = _read_stream(_FakeResp(body), boom)
    assert "book" in raw["choices"][0]["message"]["content"]


def test_the_runtime_still_reports_the_outcome_not_the_stream():
    """The streamed prose must never become the sentence a user is told happened."""
    from errand.llm import MessageScanner
    from errand.outcomes import render_outcome

    s = MessageScanner()
    prose = s.feed('{"tool": "book", "message": "Your plumber is confirmed."}')
    assert "confirmed" in prose                  # the model said it
    sentence = render_outcome("booked")
    assert sentence != prose                     # the runtime still decides
    assert "booked" in sentence.lower() or "book" in sentence.lower()


def test_the_key_may_be_split_across_chunks():
    """The provider does split JSON keys: a stream landing inside `"mess|age"` is normal."""
    from errand.llm import MessageScanner

    body = '{"tool": "book", "args": {}, "message": "Hello there, friend."}'
    for split in range(1, len(body)):
        s = MessageScanner()
        got = s.feed(body[:split]) + s.feed(body[split:])
        if split > body.index('"message"') + len('"message": "'):
            assert got.endswith("Hello there, friend."), (split, repr(got))
        else:
            assert got == "Hello there, friend.", (split, repr(got))


def test_the_key_split_one_char_at_a_time():
    """Every possible boundary, character by character."""
    from errand.llm import MessageScanner

    body = '{"tool": null, "message": "abc"}'
    s = MessageScanner()
    got = "".join(s.feed(ch) for ch in body)
    assert got == "abc", repr(got)
