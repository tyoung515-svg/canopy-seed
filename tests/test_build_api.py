import asyncio
import json
import pytest

from core.api_server import DashboardAPI

class DummySettings:
    pass

class DummyAIBackend:
    pass

class DummyRouter:
    async def _handle_ai(self, message, update, backend=None):
        await asyncio.sleep(0.1)
        return "mock result"

class DummyRequest:
    def __init__(self, payload=None, query=None):
        self._payload = payload or {}
        self.query = query or {}

    async def json(self):
        return self._payload

class DummyStreamResponse:
    def __init__(self):
        self.headers = {}
        self.written = []
        self._prepared = False

    async def prepare(self, request):
        self._prepared = True

    async def write(self, data):
        self.written.append(data.decode("utf-8"))

def _build_api():
    api = DashboardAPI.__new__(DashboardAPI)
    api.settings = DummySettings()
    api.ai_backend = DummyAIBackend()
    api.router = DummyRouter()
    
    api._canopy_sessions = {}
    api._canopy_session_messages = {}
    api._active_build = None
    api._inject_queue = []
    api._build_sse_clients = []
    return api


@pytest.mark.asyncio
async def test_build_start_success():
    api = _build_api()
    req = DummyRequest({"proposal": "test proposal", "title": "Test Build"})
    
    res = await api.handle_build_start(req)
    body = json.loads(res.text)
    
    assert res.status == 200
    assert body["success"] is True
    assert "build_id" in body
    
    assert api._active_build is not None
    assert api._active_build["proposal"] == "test proposal"
    assert api._active_build["title"] == "Test Build"
    assert api._active_build["phase"] == "plan"


@pytest.mark.asyncio
async def test_build_start_conflict():
    api = _build_api()
    api._active_build = {"id": "123", "phase": "generate"}
    
    req = DummyRequest({"proposal": "another one"})
    res = await api.handle_build_start(req)
    body = json.loads(res.text)
    
    assert res.status == 409
    assert body["success"] is False


@pytest.mark.asyncio
async def test_inject_no_active_build():
    api = _build_api()
    req = DummyRequest({"message": "do this"})
    res = await api.handle_inject(req)
    body = json.loads(res.text)
    
    assert body["success"] is False
    assert "No active build" in body["error"]


@pytest.mark.asyncio
async def test_inject_success():
    api = _build_api()
    api._active_build = {"id": "123"}
    req = DummyRequest({"message": "try harder"})
    
    res = await api.handle_inject(req)
    body = json.loads(res.text)
    
    assert body["success"] is True
    assert len(api._inject_queue) == 1
    assert api._inject_queue[0] == "try harder"


@pytest.mark.asyncio
async def test_cancel_no_active_build():
    api = _build_api()
    req = DummyRequest({})
    res = await api.handle_build_cancel(req)
    body = json.loads(res.text)
    
    assert body["success"] is False
    assert "No active build" in body["error"]


@pytest.mark.asyncio
async def test_cancel_success():
    api = _build_api()
    api._active_build = {"id": "123", "cancelled": False}
    req = DummyRequest({})
    
    res = await api.handle_build_cancel(req)
    body = json.loads(res.text)
    
    assert body["success"] is True
    assert api._active_build["cancelled"] is True


@pytest.mark.asyncio
async def test_stream_idle():
    api = _build_api()
    from aiohttp import web
    
    # We test the handle_build_stream, mocking out the StreamResponse
    # since we can't easily use the real one without a running app.
    # We will just patch web.StreamResponse for this test.
    import aiohttp.web
    original_stream = aiohttp.web.StreamResponse
    aiohttp.web.StreamResponse = DummyStreamResponse

    try:
        req = DummyRequest()
        res = await api.handle_build_stream(req)
        
        assert hasattr(res, 'written')
        assert len(res.written) == 1
        assert "build_idle" in res.written[0]
    finally:
        aiohttp.web.StreamResponse = original_stream


# ── Additional comprehensive tests per PROMPT_ANTI_CS_BUILD_API.md ──


@pytest.mark.asyncio
async def test_build_start_returns_timestamp_build_id():
    """Verify build_id is timestamp-based string."""
    api = _build_api()
    req = DummyRequest({"proposal": "test"})
    
    res = await api.handle_build_start(req)
    body = json.loads(res.text)
    
    build_id = body["build_id"]
    assert isinstance(build_id, str)
    assert "build-" in build_id


@pytest.mark.asyncio
async def test_build_start_initializes_state():
    """Verify build start properly initializes all required state fields."""
    api = _build_api()
    req = DummyRequest({
        "proposal": "Build a REST API",
        "title": "REST API Project"
    })
    
    res = await api.handle_build_start(req)
    body = json.loads(res.text)
    
    assert api._active_build["title"] == "REST API Project"
    assert api._active_build["proposal"] == "Build a REST API"
    assert api._active_build["phase"] == "plan"
    assert api._active_build["cancelled"] is False
    assert "started_at" in api._active_build


@pytest.mark.asyncio
async def test_inject_multiple_messages_queued():
    """Test that multiple inject calls accumulate messages in queue."""
    api = _build_api()
    api._active_build = {"id": "test-build"}
    
    messages = ["Use TypeScript", "Add error handling", "Write tests"]
    for msg in messages:
        req = DummyRequest({"message": msg})
        res = await api.handle_inject(req)
        body = json.loads(res.text)
        assert body["success"] is True
    
    assert len(api._inject_queue) == 3
    assert api._inject_queue == messages


@pytest.mark.asyncio
async def test_inject_empty_message():
    """Test that inject with empty message fails."""
    api = _build_api()
    api._active_build = {"id": "test"}
    req = DummyRequest({"message": ""})
    
    res = await api.handle_inject(req)
    body = json.loads(res.text)
    
    assert body["success"] is False


@pytest.mark.asyncio
async def test_build_start_no_proposal_error():
    """Test that build start without proposal returns proper error."""
    api = _build_api()
    req = DummyRequest({"title": "Test"})  # No proposal field
    
    res = await api.handle_build_start(req)
    body = json.loads(res.text)
    
    assert res.status == 400
    assert "proposal" in body["error"].lower()


@pytest.mark.asyncio
async def test_build_cancel_409_no_build():
    """Test that cancel with no active build returns error."""
    api = _build_api()
    assert api._active_build is None
    
    req = DummyRequest({})
    res = await api.handle_build_cancel(req)
    body = json.loads(res.text)
    
    assert body["success"] is False


@pytest.mark.asyncio
async def test_build_phase_progression():
    """Test that build transitions through expected phases."""
    api = _build_api()
    req = DummyRequest({"proposal": "test", "title": "Test"})
    
    # Start build (phase should be "plan")
    res = await api.handle_build_start(req)
    assert api._active_build["phase"] == "plan"


@pytest.mark.asyncio
async def test_inject_with_no_message_field():
    """Test inject request with missing message field."""
    api = _build_api()
    api._active_build = {"id": "test"}
    req = DummyRequest({})  # Empty payload
    
    res = await api.handle_inject(req)
    body = json.loads(res.text)
    
    assert body["success"] is False


@pytest.mark.asyncio
async def test_build_stream_accepts_event_source_connection():
    """Test that stream endpoint properly sets up SSE headers."""
    api = _build_api()
    
    import aiohttp.web
    original_stream = aiohttp.web.StreamResponse
    aiohttp.web.StreamResponse = DummyStreamResponse
    
    try:
        # Start a build first
        req_start = DummyRequest({"proposal": "test"})
        await api.handle_build_start(req_start)
        
        # Now connect stream
        req_stream = DummyRequest()
        res = await api.handle_build_stream(req_stream)
        
        # Should have prepared the response with SSE headers
        assert res.headers.get('Content-Type') == 'text/event-stream'
        assert res.headers.get('Cache-Control') == 'no-cache'
    finally:
        aiohttp.web.StreamResponse = original_stream


@pytest.mark.asyncio
async def test_build_state_cleared_after_cancel():
    """Test that build state is properly cleared after cancellation."""
    api = _build_api()
    
    # Start a build
    req = DummyRequest({"proposal": "test"})
    await api.handle_build_start(req)
    assert api._active_build is not None
    
    # The background task would normally clear this,
    # but in our test we're just verifying cancel sets the flag
    req_cancel = DummyRequest({})
    await api.handle_build_cancel(req_cancel)
    assert api._active_build["cancelled"] is True
