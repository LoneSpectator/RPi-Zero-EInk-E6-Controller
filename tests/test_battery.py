from io import BytesIO
from dataclasses import asdict
import json
import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from app.battery import (
    BatteryMonitor, Learning, estimate_percent, FULL_SETTLE_SECONDS,
    FULL_RECOVERY_SETTLE_SECONDS, MODEL_VERSION,
)
from app.config import Settings
from app.engine import Engine
from app.hardware import battery_status
from app.main import create_app
from app.render import battery_warning
from app.store import Store


class Samples:
    def __init__(self, voltage=3.85, current=200):
        self.now = 1000
        self.voltage, self.current = voltage, current
        self.calls = 0

    def read(self, driver):
        self.calls += 1
        return dict(available=True, voltage=self.voltage, current_ma=self.current,
                    percent=estimate_percent(self.voltage), estimated=True, message='')

    def next(self, monitor, voltage=None, current=None, seconds=4):
        self.now += seconds
        if voltage is not None:
            self.voltage = voltage
        if current is not None:
            self.current = current
        return monitor.status()


def monitor_for(tmp_path, samples):
    return BatteryMonitor(Store(tmp_path), 'waveshare', samples.read, clock=lambda: samples.now)


@pytest.mark.parametrize('current', [-1000, -1, 0, 1, 1000])
def test_ina219_signed_shunt_current_and_terminal_voltage(monkeypatch, current):
    monkeypatch.delenv('INA219_CURRENT_POLARITY', raising=False)
    class Bus:
        def __init__(self, index): assert index == 1
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read_i2c_block_data(self, addr, reg, length):
            assert addr == 0x43 and length == 2
            return {0: [0x0e, 0xef], 1: list(current.to_bytes(2, 'big', signed=True)),
                    2: [0x1c, 0xea]}[reg]  # 3.7 V at V-, conversion-ready bit set.
    monkeypatch.setitem(sys.modules, 'smbus2', SimpleNamespace(SMBus=Bus))
    result = battery_status('waveshare')
    assert result['available'] and result['current_ma'] == -current
    assert result['raw_current_ma'] == current and result['current_polarity'] == -1
    assert result['voltage'] == 3.7


@pytest.mark.parametrize('raw_current,state,charging', [
    (-222, 'discharging', False),  # actual unplugged reading reported by the owner.
    (766, 'charging', True),      # actual plugged-in reading reported by the owner.
    (0, 'idle', False),
])
def test_real_board_polarity_through_sensor_and_monitor(tmp_path, monkeypatch, raw_current, state, charging):
    monkeypatch.delenv('INA219_CURRENT_POLARITY', raising=False)
    class Bus:
        def __init__(self, index): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read_i2c_block_data(self, addr, reg, length):
            return {0: [0x0e, 0xef], 1: list(raw_current.to_bytes(2, 'big', signed=True)),
                    2: [0x1c, 0xea]}[reg]
    monkeypatch.setitem(sys.modules, 'smbus2', SimpleNamespace(SMBus=Bus))
    monitor = BatteryMonitor(Store(tmp_path), 'waveshare', battery_status)
    result = monitor.status()
    assert result['charge_state'] == state and result['charging'] is charging
    assert result['raw_current_ma'] == raw_current
    assert result['current_ma'] == -raw_current
    assert result['voltage'] == 3.7


@pytest.mark.parametrize('polarity', [-1, 1])
def test_current_polarity_override(tmp_path, monkeypatch, polarity):
    monkeypatch.setenv('INA219_CURRENT_POLARITY', str(polarity))
    class Bus:
        def __init__(self, index): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read_i2c_block_data(self, addr, reg, length):
            return {0: [0x0e, 0xef], 1: [0, 200], 2: [0x1c, 0xea]}[reg]
    monkeypatch.setitem(sys.modules, 'smbus2', SimpleNamespace(SMBus=Bus))
    result = BatteryMonitor(Store(tmp_path), 'waveshare', battery_status).status()
    assert result['current_ma'] == polarity * 200
    assert result['charge_state'] == ('discharging' if polarity == 1 else 'charging')


@pytest.mark.parametrize('legacy', [True, False], ids=['old-model', 'changed-polarity'])
def test_wrong_direction_learning_is_reset_and_persisted(tmp_path, monkeypatch, legacy):
    monkeypatch.delenv('INA219_CURRENT_POLARITY', raising=False)
    store = Store(tmp_path)
    saved = asdict(Learning(full_voltage=4.12, empty_voltage=3.12, resistance_ohm=.25,
                            charge_seen=True, charge_peak_ma=766, taper_seen=True,
                            full=True, depleted_voltage=3.1, full_calibrated_at=123, empty_calibrated_at=456))
    if legacy:
        saved.pop('model_version')
        saved.pop('current_polarity')
    else:
        saved['current_polarity'] = 1
    with store.connection() as db:
        db.execute('INSERT INTO battery_learning VALUES(1,?)', (json.dumps(saved),))
    samples = Samples()
    monitor = BatteryMonitor(store, 'waveshare', samples.read)
    assert monitor.learning == Learning()
    persisted = json.loads(store.one('SELECT payload FROM battery_learning')['payload'])
    assert persisted == asdict(Learning())
    assert persisted['model_version'] == MODEL_VERSION and persisted['current_polarity'] == -1


def test_voltage_retained_if_current_register_fails(monkeypatch):
    class Bus:
        def __init__(self, index): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read_i2c_block_data(self, addr, reg, length):
            if reg == 1:
                raise OSError('shunt read failure')
            return [0x0e, 0xef] if reg == 0 else [0x1c, 0xea]
    monkeypatch.setitem(sys.modules, 'smbus2', SimpleNamespace(SMBus=Bus))
    result = battery_status('waveshare')
    assert result['available'] and result['voltage'] == 3.7
    assert result['current_ma'] is None and 'shunt read failure' in result['current_message']


def test_curve_is_nonlinear_monotonic_clamped_and_uses_learned_endpoints():
    assert [estimate_percent(v) for v in (2.8, 3.0, 3.6, 3.7, 3.85, 4.0, 4.2, 4.3)] == [0, 0, 10, 20, 50, 80, 100, 100]
    values = [estimate_percent(v / 1000) for v in range(2800, 4301)]
    assert values == sorted(values)
    assert estimate_percent(2.95, 2.95, 4.18) == 0
    assert estimate_percent(4.18, 2.95, 4.18) == 100


def test_monitor_direction_unknown_and_cached_reads(tmp_path):
    samples = Samples(current=-200)
    monitor = monitor_for(tmp_path, samples)
    assert monitor.status()['charge_state'] == 'charging'
    assert monitor.status()['charging'] is True and samples.calls == 1
    assert samples.next(monitor, current=200)['charge_state'] == 'discharging'
    assert samples.next(monitor, current=0)['charge_state'] == 'idle'
    samples.current = None
    result = samples.next(monitor)
    assert result['charging'] is None and result['charge_state'] == 'unknown'
    assert result['available'] and result['percent'] is not None


def test_full_endpoint_requires_observed_charge_and_stable_taper_then_survives_restart(tmp_path):
    samples = Samples(voltage=4.18, current=0)
    monitor = monitor_for(tmp_path, samples)
    for _ in range(50):
        samples.next(monitor)
    assert monitor.learning.full_calibrated_at is None  # high voltage alone proves nothing.
    samples.next(monitor, current=-200)
    samples.next(monitor, current=-40)  # observe a real high-voltage taper first.
    for _ in range(FULL_SETTLE_SECONDS // 4 + 1):
        result = samples.next(monitor, current=0)
    assert result['charge_state'] == 'full'
    assert result['calibration']['full_voltage'] == 4.18
    assert result['calibration']['full_calibrated_at'] is not None
    assert result['calibration']['full_calibration_method'] == 'charge_taper'
    assert result['percent'] == 100 and result['full_confirmation'] is None
    restored = monitor_for(tmp_path, samples)
    assert restored.status()['charge_state'] == 'full'
    assert restored.learning.full_voltage == 4.18


def test_unplug_and_sampling_gap_do_not_count_as_full_settle_time(tmp_path):
    samples = Samples(voltage=4.18, current=-100)
    monitor = monitor_for(tmp_path, samples)
    monitor.status()
    samples.next(monitor, current=-20)
    samples.next(monitor, current=0)
    samples.next(monitor, seconds=300)
    assert monitor.learning.full_calibrated_at is None
    samples.next(monitor, voltage=4.15, current=250)  # unplugging supplies the load.
    for _ in range(50):
        samples.next(monitor, current=0)
    assert monitor.learning.full_calibrated_at is None


def test_abrupt_charge_stop_without_taper_cannot_use_short_confirmation(tmp_path):
    samples = Samples(voltage=4.18, current=-200)
    monitor = monitor_for(tmp_path, samples)
    monitor.status()
    for _ in range(50):
        samples.next(monitor, current=0)
    assert monitor.learning.full_calibrated_at is None
    assert monitor.status()['full_confirmation']['required_seconds'] == FULL_RECOVERY_SETTLE_SECONDS


@pytest.mark.parametrize('history', ['none', 'bulk-only', 'restart-after-bulk'])
@pytest.mark.parametrize('current', [-1, 1])
def test_reported_4116v_1ma_recovers_full_after_long_confirmation(tmp_path, history, current):
    samples = Samples(voltage=4.116, current=current)
    monitor = monitor_for(tmp_path, samples)
    monitor.learning.empty_voltage = 3.1
    if history != 'none':
        samples.current = -766
        monitor.status()
        samples.next(monitor, current=current)
        if history == 'restart-after-bulk':
            monitor = monitor_for(tmp_path, samples)
    result = monitor.status()
    assert result['percent'] == 91
    assert result['full_confirmation'] == {
        'elapsed_seconds': 0, 'required_seconds': FULL_RECOVERY_SETTLE_SECONDS, 'method': 'stable_idle',
    }
    for _ in range(FULL_RECOVERY_SETTLE_SECONDS // 4 - 1):
        result = samples.next(monitor)
    assert result['percent'] == 91 and result['calibration']['full_calibrated_at'] is None
    assert result['full_confirmation']['elapsed_seconds'] == FULL_RECOVERY_SETTLE_SECONDS - 4
    result = samples.next(monitor)
    assert result['percent'] == 100 and result['charge_state'] == 'full'
    assert result['full_confirmation'] is None
    assert result['calibration']['full_voltage'] == 4.116
    assert result['calibration']['full_calibration_method'] == 'stable_idle'
    calibrated_at = result['calibration']['full_calibrated_at']
    restored = monitor_for(tmp_path, samples)
    assert restored.status()['percent'] == 100
    assert restored.learning.empty_voltage == 3.1
    for _ in range(50):
        result = samples.next(restored)
    assert result['charge_state'] == 'full' and result['full_confirmation'] is None
    assert result['calibration']['full_calibrated_at'] == calibrated_at


@pytest.mark.parametrize('interruption', ['gap', 'unavailable', 'charging', 'discharging', 'voltage-jump', 'current-unknown'])
def test_recovery_requires_a_fresh_continuous_interval_after_interruption(tmp_path, interruption):
    samples = Samples(voltage=4.116, current=-1)
    monitor = monitor_for(tmp_path, samples)
    monitor.status()
    for _ in range(FULL_RECOVERY_SETTLE_SECONDS // 4 - 1):
        samples.next(monitor)
    if interruption == 'gap':
        samples.next(monitor, seconds=300)
    elif interruption == 'unavailable':
        reader = monitor.reader
        monitor.reader = lambda driver: {'available': False}
        assert samples.next(monitor)['full_confirmation'] is None
        monitor.reader = reader
    elif interruption == 'charging':
        samples.next(monitor, current=-50)
    elif interruption == 'discharging':
        samples.next(monitor, current=50)
    elif interruption == 'voltage-jump':
        samples.next(monitor, voltage=4.15)
    else:
        samples.current = None
        assert samples.next(monitor)['full_confirmation'] is None
    result = samples.next(monitor, voltage=4.116, current=-1)
    assert result['calibration']['full_calibrated_at'] is None
    assert result['full_confirmation']['elapsed_seconds'] <= 4
    for _ in range(FULL_RECOVERY_SETTLE_SECONDS // 4 - 2):
        result = samples.next(monitor)
    assert result['calibration']['full_calibrated_at'] is None
    samples.next(monitor)
    result = samples.next(monitor)
    assert result['charge_state'] == 'full' and result['percent'] == 100


@pytest.mark.parametrize('voltage,current', [(4.096, -1), (4.304, -1), (4.116, -11), (4.116, 11), (4.116, None)])
def test_ineligible_voltage_or_current_never_recovers_full(tmp_path, voltage, current):
    samples = Samples(voltage=voltage, current=current)
    monitor = monitor_for(tmp_path, samples)
    monitor.status()
    for _ in range(FULL_RECOVERY_SETTLE_SECONDS // 4 + 5):
        result = samples.next(monitor)
    assert result['calibration']['full_calibrated_at'] is None
    assert result['full_confirmation'] is None
    assert result['charge_state'] != 'full'


def test_restart_preserves_old_endpoints_but_does_not_count_offline_confirmation_time(tmp_path):
    samples = Samples(voltage=4.116, current=-1)
    monitor = monitor_for(tmp_path, samples)
    monitor.learning.empty_voltage = 3.1
    monitor.status()
    for _ in range(100):
        samples.next(monitor)
    # Version 2 data from before full_calibration_method existed remains valid.
    with monitor.store.connection() as db:
        saved = json.loads(db.execute('SELECT payload FROM battery_learning').fetchone()[0])
        saved.pop('full_calibration_method')
        db.execute('UPDATE battery_learning SET payload=?', (json.dumps(saved),))
    samples.now += 3600
    restored = monitor_for(tmp_path, samples)
    result = restored.status()
    assert restored.learning.empty_voltage == 3.1
    assert result['full_confirmation']['elapsed_seconds'] == 0
    for _ in range(FULL_RECOVERY_SETTLE_SECONDS // 4 - 1):
        result = samples.next(restored)
    assert result['calibration']['full_calibrated_at'] is None
    assert samples.next(restored)['charge_state'] == 'full'


@pytest.mark.parametrize('swing,calibrates', [(.020, True), (.024, False)])
def test_voltage_stability_boundary_and_confirmed_full_percentage(tmp_path, swing, calibrates):
    samples = Samples(voltage=4.116 - swing / 2, current=-1)
    monitor = monitor_for(tmp_path, samples)
    monitor.status()
    for index in range(FULL_RECOVERY_SETTLE_SECONDS // 4):
        result = samples.next(monitor, voltage=4.116 + (-1 if index % 2 else 1) * swing / 2)
    assert (result['charge_state'] == 'full') is calibrates
    if calibrates:
        assert result['percent'] == 100
        assert result['calibration']['full_voltage'] == 4.116
    else:
        assert result['calibration']['full_calibrated_at'] is None


def test_depleted_endpoint_requires_sustained_low_discharge_then_next_charge(tmp_path):
    samples = Samples(voltage=3.05, current=200)
    monitor = monitor_for(tmp_path, samples)
    monitor.status()
    samples.next(monitor, seconds=4)
    samples.next(monitor, voltage=3.7)
    samples.next(monitor, voltage=3.8, current=-200)
    assert monitor.learning.empty_calibrated_at is None
    for _ in range(10):
        samples.next(monitor, voltage=3.05, current=200)
    assert monitor.learning.depleted_voltage == 3.05
    assert monitor.learning.empty_calibrated_at is None
    restored = monitor_for(tmp_path, samples)  # persisted before battery cutoff/restart.
    result = samples.next(restored, voltage=3.4, current=-200)
    assert result['calibration']['empty_voltage'] == 3.05
    assert result['calibration']['empty_calibrated_at'] is not None


def test_resistance_learned_from_repeated_load_steps_and_voltage_smoothed(tmp_path):
    samples = Samples(voltage=3.86, current=200)  # OCV 3.9 V, resistance 0.2 ohm.
    monitor = monitor_for(tmp_path, samples)
    monitor.status()
    for current in (600, 300, 700):
        result = samples.next(monitor, voltage=3.9 - current / 1000 * .2, current=current)
    assert monitor.learning.resistance_ohm == pytest.approx(.2)
    assert result['ocv_voltage'] == 3.9
    before = result['percent']
    result = samples.next(monitor, voltage=4.1, current=-200)
    assert abs(result['percent'] - before) < 10  # charger insertion must not jump straight to 90%.


def test_border_is_exactly_five_pixels_and_does_not_modify_source():
    source = Image.new('RGB', (800, 480), 'white')
    image = battery_warning(source, True)
    for point in [(0, 0), (4, 240), (795, 240), (400, 4), (400, 475)]:
        assert image.getpixel(point) == (255, 0, 0)
    for point in [(5, 240), (794, 240), (400, 5), (400, 474)]:
        assert image.getpixel(point) == (255, 255, 255)
    assert source.getpixel((0, 0)) == (255, 255, 255)
    assert battery_warning(source, False) is source


class Battery:
    def __init__(self, percent=50): self.percent = percent
    def status(self): return dict(available=self.percent is not None, percent=self.percent)


class Display:
    def __init__(self): self.images = []
    def display(self, image): self.images.append(image.copy())


def screen_engine(tmp_path):
    store = Store(tmp_path)
    with store.connection() as db:
        db.execute("INSERT INTO contents VALUES('c','原始画面','image',0,NULL,'{}')")
    Image.new('RGB', (800, 480), 'white').save(store.image_path('c'))
    display = Display()
    engine = Engine(store, Settings(data_dir=tmp_path, password='', driver='mock'), display)
    engine.battery = Battery()
    engine.enqueue('c')
    assert engine.process_one()
    return engine, store, display


def test_automatic_border_deduplicates_and_restores_deleted_library_picture(tmp_path, monkeypatch):
    engine, store, display = screen_engine(tmp_path)
    with store.connection() as db:
        db.execute("DELETE FROM contents WHERE id='c'")
    store.image_path('c').unlink()
    engine.battery.percent = 19
    engine.update_battery_warning(); engine.update_battery_warning()
    assert len(store.rows("SELECT * FROM jobs WHERE status='pending'")) == 1
    assert engine.process_one()
    assert display.images[-1].getpixel((0, 0)) == (255, 0, 0)
    assert store.one('SELECT * FROM display_state')['battery_warning'] == 1
    engine.update_battery_warning()
    assert not store.rows("SELECT * FROM jobs WHERE status='pending'")
    engine.battery.percent = None
    engine.update_battery_warning()
    assert engine.low_battery() is True
    monkeypatch.setattr('app.engine.now_ts', lambda: 1000)
    engine.battery.percent = 20
    engine.update_battery_warning()
    assert not store.rows("SELECT * FROM jobs WHERE status='pending'")
    monkeypatch.setattr('app.engine.now_ts', lambda: 1030)
    engine.update_battery_warning()
    assert engine.process_one()
    assert display.images[-1].tobytes() == display.images[0].tobytes()
    assert store.one('SELECT * FROM display_state')['battery_warning'] == 0
    assert len(list(store.root.glob('screen-*.png'))) == 2  # one pristine source, one displayed snapshot.


def test_normal_queue_takes_priority_and_low_border_applies_to_latest_content(tmp_path):
    engine, store, display = screen_engine(tmp_path)
    engine.enqueue('c')
    engine.battery.percent = 5
    engine.update_battery_warning()
    assert len(store.rows("SELECT * FROM jobs WHERE status='pending'")) == 1
    assert engine.process_one()
    assert display.images[-1].getpixel((0, 0)) == (255, 0, 0)
    assert Image.open(store.image_path('c')).getpixel((0, 0)) == (255, 255, 255)


def test_failed_automatic_warning_does_not_repeatedly_refresh(tmp_path):
    engine, store, display = screen_engine(tmp_path)
    class BrokenDisplay:
        def display(self, image):
            raise OSError('SPI disconnected')
    engine.display = BrokenDisplay()
    engine.battery.percent = 5
    engine.update_battery_warning()
    assert engine.process_one()
    assert store.one('SELECT * FROM display_state')['status'] == 'error'
    for _ in range(3):
        engine.update_battery_warning()
    assert not store.rows("SELECT * FROM jobs WHERE status='pending'")
    assert len(store.rows('SELECT * FROM jobs')) == 2
    assert display.images[-1].getpixel((0, 0)) == (255, 255, 255)


def test_low_battery_preview_matches_physical_warning_without_persisting_border(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, password='', driver='mock'))
    with TestClient(app) as client:
        app.state.engine.battery = Battery(19)
        doc = {'paragraphs': [{'runs': [{'text': '低电量预览'}]}]}
        response = client.post('/api/preview/text', json=doc)
        assert response.status_code == 200
        assert Image.open(BytesIO(response.content)).getpixel((0, 0)) == (255, 0, 0)
        saved = client.post('/api/contents/text', json=doc)
        assert saved.status_code == 201
        image = client.get('/api/contents/' + saved.json()['id'] + '/image')
        assert Image.open(BytesIO(image.content)).getpixel((0, 0)) == (255, 255, 255)


def test_old_schema_upgrade_preserves_content_and_adds_defaults(tmp_path):
    store = Store(tmp_path)
    with store.connection() as db:
        db.execute("INSERT INTO contents VALUES('c','保留','image',0,NULL,'{}')")
        db.execute("ALTER TABLE jobs DROP COLUMN source_snapshot")
        db.execute("ALTER TABLE jobs DROP COLUMN purpose")
        db.execute("ALTER TABLE display_state DROP COLUMN source_snapshot")
        db.execute("ALTER TABLE display_state DROP COLUMN battery_warning")
        db.execute('DROP TABLE battery_learning')
    restored = Store(tmp_path)
    assert restored.one('SELECT * FROM contents')['title'] == '保留'
    assert restored.one('SELECT * FROM display_state')['battery_warning'] == 0
    assert restored.rows('SELECT * FROM battery_learning') == []
