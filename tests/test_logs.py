from datetime import datetime, timezone
from io import BytesIO
import time

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from app.config import Settings
from app.main import create_app
from app.store import Store, now_ts


DAY = 86400


def insert_jobs(store, count, start=None):
    start = now_ts() - DAY if start is None else start
    with store.connection() as db:
        for index in range(count):
            stamp = start + index
            db.execute('INSERT INTO jobs(id,title,status,error,created_at,finished_at,scheduled_for,purpose) '
                       'VALUES(?,?,?,?,?,?,?,?)',
                       (f'log-{index:03}', f'刷新 {index:03}', 'failed' if index % 7 == 0 else 'done',
                        'SPI error' if index % 7 == 0 else None, stamp, stamp + 1,
                        stamp if index % 3 == 1 else None, 'battery_warning' if index % 3 == 2 else 'display'))


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, driver='mock', password='', mock_delay=.01, timezone='Asia/Shanghai')


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client


def test_history_empty_defaults(client):
    result = client.get('/api/jobs').json()
    assert result == {'items': [], 'total': 0, 'page': 1, 'page_size': 20, 'total_pages': 1,
                      'retention_days': 7, 'timezone': 'Asia/Shanghai'}
    assert client.get('/api/settings/logs').json() == {'retention_days': 7}


@pytest.mark.parametrize('page_size', [20, 50, 100])
def test_complete_history_paginates_and_recent_is_limited_to_five(client, app, page_size):
    insert_jobs(app.state.store, 105)
    first = client.get(f'/api/jobs?page_size={page_size}').json()
    assert first['total'] == 105 and len(first['items']) == page_size
    assert [item['id'] for item in first['items']] == [f'log-{i:03}' for i in range(104, 104 - page_size, -1)]
    second = client.get(f'/api/jobs?page=2&page_size={page_size}').json()
    assert not {item['id'] for item in first['items']} & {item['id'] for item in second['items']}
    last = client.get(f'/api/jobs?page=9999999999999999&page_size={page_size}').json()
    assert last['page'] == last['total_pages'] == (105 + page_size - 1) // page_size
    assert last['items'][-1]['id'] == 'log-000'
    recent = client.get('/api/status').json()['jobs']
    assert len(recent) == 5
    assert [item['id'] for item in recent] == [f'log-{i:03}' for i in range(104, 99, -1)]


def test_history_order_uses_record_time_and_stable_ties(client, app):
    now = now_ts()
    with app.state.store.connection() as db:
        for job_id in ('first', 'second'):
            db.execute("INSERT INTO jobs(id,title,status,created_at,finished_at) VALUES(?,?,'done',?,?)",
                       (job_id, job_id, now - 2 * DAY, now - 1))
        db.execute("INSERT INTO jobs(id,title,status,created_at,finished_at) VALUES('later-created','title','done',?,?)",
                   (now - DAY, now - 5))
    result = client.get('/api/jobs').json()
    assert [item['id'] for item in result['items']] == ['second', 'first', 'later-created']


@pytest.mark.parametrize('query', ['page=0', 'page=-1', 'page=x', 'page_size=0', 'page_size=5', 'page_size=25', 'page_size=101'])
def test_invalid_history_pagination(client, query):
    assert client.get('/api/jobs?' + query).status_code == 422


def test_retention_expires_only_terminal_jobs_and_keeps_boundary_and_recent_completion(tmp_path, monkeypatch):
    store = Store(tmp_path)
    anchor = int(datetime(2030, 1, 20, tzinfo=timezone.utc).timestamp())
    monkeypatch.setattr('app.store.now_ts', lambda: anchor)
    old = anchor - 8 * DAY
    rows = [('old-done', 'done', old, old), ('old-failed', 'failed', old, old),
            ('legacy-failed', 'failed', old, None), ('pending', 'pending', old, None),
            ('running', 'running', old, None), ('recent-finish', 'done', old, anchor - 1),
            ('boundary', 'done', anchor - 7 * DAY, anchor - 7 * DAY)]
    with store.connection() as db:
        for job_id, status, created, finished in rows:
            db.execute('INSERT INTO jobs(id,title,status,created_at,finished_at) VALUES(?,?,?,?,?)',
                       (job_id, job_id, status, created, finished))
    visible = store.job_history()
    assert {item['id'] for item in visible['items']} == {'pending', 'running', 'recent-finish', 'boundary'}
    assert store.prune_jobs() == 3
    assert {item['id'] for item in store.rows('SELECT * FROM jobs')} == {'pending', 'running', 'recent-finish', 'boundary'}
    assert store.prune_jobs() == 0


def test_retention_setting_is_persistent_and_shortening_it_prunes_immediately(settings):
    with TestClient(create_app(settings)) as client:
        store = client.app.state.store
        insert_jobs(store, 1, now_ts() - 10 * DAY)
        assert client.get('/api/jobs').json()['total'] == 0
        response = client.put('/api/settings/logs', json={'retention_days': 14})
        assert response.status_code == 200 and response.json() == {'retention_days': 14}
        assert client.get('/api/jobs').json()['total'] == 1
    with TestClient(create_app(settings)) as client:
        assert client.get('/api/settings/logs').json() == {'retention_days': 14}
        assert client.get('/api/jobs').json()['total'] == 1
        assert client.put('/api/settings/logs', json={'retention_days': 1}).status_code == 200
        assert client.get('/api/jobs').json()['total'] == 0
        assert client.app.state.store.rows('SELECT * FROM jobs') == []


@pytest.mark.parametrize('days', [0, -1, 1.5, True, '7', None, 36501])
def test_invalid_retention_does_not_change_setting(client, days):
    assert client.put('/api/settings/logs', json={'retention_days': days}).status_code == 422
    assert client.get('/api/settings/logs').json() == {'retention_days': 7}


def test_startup_and_periodic_cleanup(settings):
    store = Store(settings.data_dir)
    insert_jobs(store, 1, now_ts() - 8 * DAY)
    with TestClient(create_app(settings)) as client:
        store = client.app.state.store
        assert store.rows('SELECT * FROM jobs') == []
        insert_jobs(store, 1, now_ts() - 8 * DAY)
        client.app.state.engine.next_log_cleanup = 0
        deadline = time.monotonic() + 3
        while store.rows('SELECT * FROM jobs') and time.monotonic() < deadline:
            time.sleep(.03)
        assert store.rows('SELECT * FROM jobs') == []


def test_expired_history_does_not_remove_displayed_snapshot(client, app):
    data = BytesIO(); Image.new('RGB', (800, 480), 'blue').save(data, 'PNG')
    item = client.post('/api/contents/image', files={'file': ('image.png', data.getvalue(), 'image/png')}, data={'title': '保留画面'}).json()
    job_id = client.post('/api/contents/' + item['id'] + '/display').json()['job_id']
    deadline = time.monotonic() + 3
    while client.get('/api/jobs/' + job_id).json()['status'] != 'done':
        assert time.monotonic() < deadline
        time.sleep(.02)
    before = client.get('/api/screen').content
    state = app.state.store.one('SELECT * FROM display_state')
    with app.state.store.connection() as db:
        db.execute('UPDATE jobs SET created_at=?,finished_at=? WHERE id=?', (now_ts() - 8 * DAY, now_ts() - 8 * DAY, job_id))
    assert client.put('/api/settings/logs', json={'retention_days': 7}).status_code == 200
    assert client.get('/api/jobs/' + job_id).status_code == 404
    assert client.get('/api/screen').content == before
    assert (app.state.store.root / state['source_snapshot']).is_file()
    assert client.get('/api/status').json()['screen']['title'] == '保留画面'


def test_history_and_retention_require_auth_and_mutations_reject_cross_origin(settings):
    settings.password = 'test-password'
    with TestClient(create_app(settings)) as client:
        for path in ('/api/jobs', '/api/settings/logs'):
            assert client.get(path).status_code == 401
        assert client.put('/api/settings/logs', json={'retention_days': 3}).status_code == 401
        client.auth = ('admin', 'test-password')
        assert client.get('/api/jobs').status_code == 200
        response = client.put('/api/settings/logs', json={'retention_days': 3}, headers={'Origin': 'https://elsewhere.example'})
        assert response.status_code == 403
        assert client.get('/api/settings/logs').json()['retention_days'] == 7
