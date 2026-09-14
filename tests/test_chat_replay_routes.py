import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from core.database import Base, Session as DbSession
from routes import session_routes
from routes.chat_replay_routes import setup_chat_replay_routes
from src.chat_replay_log import ReplayLog
from src import agent_runs


@pytest.fixture
def client(monkeypatch, tmp_path):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[DbSession.__table__])
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(DbSession(id='chat-a', name='A', model='local', endpoint_url='http://local/v1', owner='alice'))
        db.add(DbSession(id='chat-b', name='B', model='local', endpoint_url='http://local/v1', owner='bob'))
        db.commit()
    monkeypatch.setattr(session_routes, 'SessionLocal', factory)
    monkeypatch.setattr(agent_runs, 'replay_root', lambda: tmp_path)
    monkeypatch.setenv('ODYSSEUS_DURABLE_CHAT_REPLAY', '1')
    log = ReplayLog(tmp_path, 'a' * 32, 'chat-a', create=True)
    log.append('data: private result\n\n')
    app = FastAPI()

    @app.middleware('http')
    async def owner(request, call_next):
        request.state.current_user = request.headers.get('X-Test-User', 'alice')
        return await call_next(request)

    app.include_router(setup_chat_replay_routes())
    with TestClient(app) as client:
        yield client
    engine.dispose()


def test_owner_can_read_after_restart_but_other_owner_cannot(client):
    path = '/api/chat/replay/chat-a?run_id=' + 'a' * 32
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'private, no-store'
    assert response.json()['status'] == 'interrupted'
    assert response.json()['events'][0]['event'] == 'data: private result\n\n'
    foreign = client.get(path, headers={'X-Test-User': 'bob'})
    assert foreign.status_code == 404
    assert 'private result' not in foreign.text
    swapped = client.get('/api/chat/replay/chat-b?run_id=' + 'a' * 32, headers={'X-Test-User': 'bob'})
    assert swapped.status_code == 404


def test_off_flag_and_cursor_bounds(client, monkeypatch):
    path = '/api/chat/replay/chat-a?run_id=' + 'a' * 32
    assert client.get(path + '&after_seq=99999').status_code == 400
    assert client.get(path + '&limit=101').status_code == 400
    assert client.get('/api/chat/replay/chat-a?run_id=../secret').status_code == 400
    monkeypatch.delenv('ODYSSEUS_DURABLE_CHAT_REPLAY')
    assert client.get(path).status_code == 404
