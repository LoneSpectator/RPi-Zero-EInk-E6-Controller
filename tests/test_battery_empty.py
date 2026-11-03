import json

import pytest

from app.battery import BatteryMonitor, EMPTY_RESERVE_VOLTAGE, estimate_percent
from app.config import Settings
from app.engine import Engine
from app.store import Store


class Samples:
    def __init__(self, voltage=3.5, current=200):
        self.now, self.voltage, self.current, self.calls = 1000, voltage, current, 0

    def read(self, driver):
        self.calls += 1
        return {'available': True, 'voltage': self.voltage, 'current_ma': self.current, 'estimated': True}

    def step(self, monitor, voltage=None, current=None, seconds=1):
        self.now += seconds
        if voltage is not None:
            self.voltage = voltage
        if current is not None:
            self.current = current
        return monitor.status()


def monitor_for(root, samples, boot='boot-a'):
    return BatteryMonitor(Store(root), 'waveshare', samples.read,
                          clock=lambda: samples.now, boot_id_reader=lambda: boot)


def low_history(root, final_voltage=3.28, reference_voltage=3.5):
    samples = Samples(reference_voltage)
    monitor = monitor_for(root, samples)
    monitor.status()
    for _ in range(360):
        samples.step(monitor)
    for _ in range(30):
        samples.step(monitor, voltage=3.3)
    samples.step(monitor, voltage=final_voltage, seconds=5)
    return samples, monitor


def test_low_voltage_increases_sampling_and_durable_recording_frequency(tmp_path):
    samples = Samples(voltage=3.9)
    monitor = monitor_for(tmp_path, samples)
    monitor.status()
    samples.step(monitor)
    assert samples.calls == 1  # normal readings remain cached for four seconds.
    samples.step(monitor, voltage=3.55, seconds=3)
    assert len(Store(tmp_path).rows('SELECT * FROM battery_samples')) == 1
    samples.step(monitor)
    assert len(Store(tmp_path).rows('SELECT * FROM battery_samples')) == 3
    for _ in range(4):
        samples.step(monitor)
    assert len(Store(tmp_path).rows('SELECT * FROM battery_samples')) == 3  # buffered, not one commit per second.
    samples.step(monitor)
    assert samples.calls == 8
    rows = monitor.store.rows('SELECT recorded_at,voltage FROM battery_samples ORDER BY recorded_at')
    assert [row['recorded_at'] for row in rows] == [1000, 1004, 1005, 1006, 1007, 1008, 1009, 1010]
    assert all(row['voltage'] == 3.55 for row in rows[1:])
    # Re-open SQLite independently: the trace isn't merely held in RAM.
    assert len(Store(tmp_path).rows('SELECT * FROM battery_samples')) == 8
    calls = []
    monitor.reader = lambda driver: calls.append(True) or {'available': False}
    for _ in range(3):
        assert not samples.step(monitor)['available']
    assert len(calls) == 3  # sensor errors do not slow retries in the low region.
    assert len(monitor.store.rows('SELECT * FROM battery_samples')) == 8


def test_normal_history_is_recorded_every_thirty_seconds(tmp_path):
    samples = Samples(voltage=3.9)
    monitor = monitor_for(tmp_path, samples)
    monitor.status()
    for _ in range(8):
        samples.step(monitor, seconds=4)
    assert samples.calls == 9
    assert [row['recorded_at'] for row in monitor.store.rows('SELECT recorded_at FROM battery_samples ORDER BY id')] == [1000, 1032]


def test_reserve_reaches_zero_before_deep_discharge_without_smoothing_delay(tmp_path):
    samples = Samples(voltage=3.85)
    monitor = monitor_for(tmp_path, samples)
    assert monitor.status()['percent'] == 48
    result = samples.step(monitor, voltage=3.44, seconds=4)
    assert result['percent'] == 0 and result['low']
    assert monitor.filtered_ocv > EMPTY_RESERVE_VOLTAGE
    assert monitor.learning.empty_calibrated_at is None  # reserve is a policy, not a fabricated measurement.
    assert estimate_percent(3.7, 3.45, 4.2) == 16
    assert estimate_percent(3.85, 3.45, 4.2) == 48  # mid-charge curve shape remains intact.


@pytest.mark.parametrize('last_voltage', [3.12, 3.05, 2.5])
def test_critical_sample_uses_earlier_window_immediately_not_the_final_collapse(tmp_path, last_voltage):
    samples, monitor = low_history(tmp_path, final_voltage=last_voltage)
    pending = monitor.learning.pending_empty
    assert pending['voltage'] == 3.52 and pending['reference_voltage'] == 3.5
    assert pending['window_end'] <= samples.now - 180
    assert pending['window_start'] >= samples.now - 300
    assert pending['sample_count'] >= 3 and pending['reason'] == 'critical_discharge'
    persisted = json.loads(Store(tmp_path).one('SELECT payload FROM battery_learning')['payload'])
    assert persisted['pending_empty'] == pending
    assert monitor.learning.empty_calibrated_at is None
    result = samples.step(monitor, voltage=3.7, current=-200)
    assert result['calibration']['empty_voltage'] == 3.52
    assert result['calibration']['empty_calibration_method'] == 'pre_cutoff_window'
    assert monitor.learning.pending_empty is None


def test_unclean_host_reboot_recovers_history_even_before_315v_was_observed(tmp_path):
    samples, monitor = low_history(tmp_path, final_voltage=3.28)
    assert monitor.learning.pending_empty is None
    old_session = monitor.session_id
    samples.now += 60
    samples.voltage, samples.current = 3.7, -200
    restored = monitor_for(tmp_path, samples, boot='boot-b')
    session = restored.store.one('SELECT * FROM battery_sessions WHERE id=?', (restored.session_id,))
    assert session['restart_reason'] == 'unclean_reboot' and session['previous_session_id'] == old_session
    result = restored.status()
    assert result['calibration']['empty_voltage'] == 3.52
    reference = result['calibration']['empty_reference']
    assert reference['reason'] == 'unclean_reboot' and reference['source_session'] == old_session
    assert reference['window_end'] <= reference['last_sample_at'] - 180


def test_reboot_after_offline_charging_applies_reference_on_full_confirmation(tmp_path):
    samples, monitor = low_history(tmp_path, final_voltage=3.28)
    samples.now += 3600
    samples.voltage, samples.current = 4.116, -1
    restored = monitor_for(tmp_path, samples, boot='boot-b')
    assert restored.learning.pending_empty is not None
    restored.status()
    for _ in range(225):
        result = samples.step(restored, seconds=4)
    assert result['charge_state'] == 'full' and result['percent'] == 100
    assert result['calibration']['empty_voltage'] == 3.52
    assert result['calibration']['full_voltage'] == 4.116
    assert result['calibration']['pending_empty'] is None


def test_power_loss_after_sample_commit_does_not_require_the_last_learning_write(tmp_path, monkeypatch):
    samples, monitor = low_history(tmp_path, final_voltage=3.28)
    def fail_save(now):
        raise OSError('power lost after durable sample commit')
    monkeypatch.setattr(monitor, '_save', fail_save)
    with pytest.raises(OSError, match='power lost'):
        samples.step(monitor, voltage=2.5, seconds=5)
    saved = json.loads(monitor.store.one('SELECT payload FROM battery_learning')['payload'])
    assert saved['pending_empty'] is None
    assert monitor.store.one('SELECT voltage FROM battery_samples ORDER BY id DESC LIMIT 1')['voltage'] == 2.5
    samples.now += 60
    samples.voltage, samples.current = 3.7, -200
    restored = monitor_for(tmp_path, samples, boot='boot-b')
    assert restored.status()['calibration']['empty_voltage'] == 3.52


def test_loss_of_the_last_unflushed_batch_still_recovers_the_earlier_reference(tmp_path):
    samples, monitor = low_history(tmp_path, final_voltage=3.28)
    for voltage in (3.14, 3.08, 3.0, 2.7):
        samples.step(monitor, voltage=voltage)
    # The four uncommitted seconds disappear with the old process. They are
    # neither needed as a trigger nor selected as the calibration voltage.
    assert monitor.store.one('SELECT voltage FROM battery_samples ORDER BY id DESC LIMIT 1')['voltage'] == 3.28
    samples.now += 60
    samples.voltage, samples.current = 3.7, -200
    restored = monitor_for(tmp_path, samples, boot='boot-b')
    result = restored.status()['calibration']
    assert result['empty_voltage'] == 3.52
    assert result['empty_reference']['reference_voltage'] == 3.5


@pytest.mark.parametrize('clean,new_boot', [(True, 'boot-a'), (True, 'boot-b'), (False, 'boot-a'), (False, None)])
def test_normal_restart_or_missing_host_reboot_evidence_does_not_calibrate(tmp_path, clean, new_boot):
    samples, monitor = low_history(tmp_path, final_voltage=3.28)
    if clean:
        monitor.close()
        assert monitor.store.one('SELECT ended_at FROM battery_sessions WHERE id=?', (monitor.session_id,))['ended_at'] is not None
    samples.now += 60
    samples.voltage, samples.current = 3.7, -200
    restored = monitor_for(tmp_path, samples, boot=new_boot)
    result = restored.status()
    assert result['calibration']['empty_calibrated_at'] is None
    assert result['calibration']['empty_voltage'] == EMPTY_RESERVE_VOLTAGE
    assert result['calibration']['pending_empty'] is None


def test_insufficient_history_keeps_reserve_instead_of_using_the_last_value(tmp_path):
    samples = Samples(voltage=3.3)
    monitor = monitor_for(tmp_path, samples)
    monitor.status()
    for _ in range(5):
        samples.step(monitor)
    samples.step(monitor, voltage=3.05)
    samples.now += 60
    samples.voltage, samples.current = 3.7, -200
    restored = monitor_for(tmp_path, samples, boot='boot-b')
    result = restored.status()
    assert result['calibration']['empty_voltage'] == 3.45
    assert result['calibration']['empty_calibrated_at'] is None


def test_recovered_load_dip_clears_pending_calibration(tmp_path):
    samples, monitor = low_history(tmp_path, final_voltage=3.05)
    assert monitor.learning.pending_empty is not None
    samples.step(monitor, voltage=3.8)
    assert monitor.learning.pending_empty is None
    result = samples.step(monitor, voltage=3.9, current=-200, seconds=4)
    assert result['calibration']['empty_calibrated_at'] is None


@pytest.mark.parametrize('current', [200, -200])
def test_recovery_and_charger_transitions_are_durable_before_the_next_normal_interval(tmp_path, current):
    samples, monitor = low_history(tmp_path, final_voltage=3.28)
    samples.step(monitor, voltage=3.8, current=current)
    for _ in range(4):
        samples.step(monitor)
    last = monitor.store.one('SELECT * FROM battery_samples ORDER BY id DESC LIMIT 1')
    assert last['voltage'] == 3.8 and last['current_ma'] == current
    samples.now += 60
    samples.voltage, samples.current = 3.9, -200
    restored = monitor_for(tmp_path, samples, boot='boot-b')
    assert restored.status()['calibration']['empty_calibrated_at'] is None


def test_empty_updates_never_reduce_an_existing_reserve(tmp_path):
    samples, monitor = low_history(tmp_path, final_voltage=3.05)
    monitor.learning.empty_voltage = 3.58
    result = samples.step(monitor, voltage=3.7, current=-200)
    assert result['calibration']['empty_voltage'] == 3.58
    assert result['calibration']['empty_reference']['reference_voltage'] == 3.5


def test_unstable_reference_window_is_rejected(tmp_path):
    samples = Samples(voltage=3.4)
    monitor = monitor_for(tmp_path, samples)
    monitor.status()
    for index in range(360):
        samples.step(monitor, voltage=3.58 if index % 2 else 3.4)
    for _ in range(30):
        samples.step(monitor, voltage=3.3)
    samples.step(monitor, voltage=3.05)
    assert monitor.learning.pending_empty is None
    assert samples.step(monitor, voltage=3.7, current=-200)['calibration']['empty_calibrated_at'] is None


def test_reference_is_three_to_five_minutes_earlier_not_the_recent_low_plateau(tmp_path):
    samples = Samples(voltage=3.55)
    monitor = monitor_for(tmp_path, samples)
    monitor.status()
    for _ in range(360):
        samples.step(monitor)
    for _ in range(150):
        samples.step(monitor, voltage=3.3)
    samples.step(monitor, voltage=3.05, seconds=5)
    pending = monitor.learning.pending_empty
    assert pending['reference_voltage'] == 3.55 and pending['voltage'] == 3.57
    assert samples.now - 300 <= pending['window_start'] < pending['window_end'] <= samples.now - 180


def test_history_is_bounded_without_pruning_the_active_session(tmp_path):
    samples = Samples(voltage=3.9)
    monitor = monitor_for(tmp_path, samples)
    monitor.status()
    old_session = monitor.session_id
    samples.now += 48 * 3600 + 61
    restored = monitor_for(tmp_path, samples)
    restored.status()
    samples.step(restored, seconds=61)
    assert restored.store.one('SELECT id FROM battery_sessions WHERE id=?', (old_session,)) is None
    assert restored.store.one('SELECT id FROM battery_sessions WHERE id=?', (restored.session_id,)) is not None
    assert all(row['session_id'] == restored.session_id for row in restored.store.rows('SELECT * FROM battery_samples'))


def test_upgrade_preserves_full_calibration_and_discards_legacy_final_minimum(tmp_path):
    store = Store(tmp_path)
    legacy = {'model_version': 2, 'current_polarity': -1, 'full_voltage': 4.116,
              'full_calibrated_at': 123, 'empty_voltage': 3.05, 'empty_calibrated_at': 234,
              'depleted_voltage': 2.95}
    with store.connection() as db:
        db.execute('INSERT INTO battery_learning VALUES(1,?)', (json.dumps(legacy),))
    samples = Samples(voltage=3.7, current=-200)
    monitor = monitor_for(tmp_path, samples)
    result = monitor.status()['calibration']
    assert result['full_voltage'] == 4.116 and result['full_calibrated_at'] == 123
    assert result['empty_voltage'] == 3.45 and result['empty_calibrated_at'] is None
    assert result['pending_empty'] is None and result['empty_calibration_method'] == 'reserve_floor'


def test_normal_engine_shutdown_marks_the_battery_session_clean(tmp_path):
    samples = Samples()
    engine = Engine(Store(tmp_path), Settings(data_dir=tmp_path, driver='mock', bing_default_wallpaper=False))
    engine.battery = monitor_for(tmp_path, samples)
    engine.battery.status()
    engine.stop()
    session = engine.store.one('SELECT * FROM battery_sessions WHERE id=?', (engine.battery.session_id,))
    assert session['ended_at'] == samples.now


def test_clean_shutdown_flushes_the_partial_five_second_batch(tmp_path):
    samples = Samples()
    monitor = monitor_for(tmp_path, samples)
    monitor.status()
    samples.step(monitor)
    assert len(Store(tmp_path).rows('SELECT * FROM battery_samples')) == 1
    monitor.close()
    assert len(Store(tmp_path).rows('SELECT * FROM battery_samples')) == 2
