"""Single-cell Li-ion voltage model and persistent, conservative cycle learning."""
from dataclasses import asdict, dataclass
import json
import logging
import math
import os
from pathlib import Path
from statistics import median
from threading import Lock
import time

from .store import new_id


log = logging.getLogger(__name__)
LOW_BATTERY_PERCENT = 20
CURRENT_DEADBAND_MA = 10
SAMPLE_SECONDS = 4
FULL_SETTLE_SECONDS = 180
FULL_RECOVERY_SETTLE_SECONDS = 15 * 60
LOW_SAMPLE_SECONDS = 1
LOW_RECORD_SECONDS = 5
LOW_RECORD_VOLTAGE = 3.6
NORMAL_RECORD_SECONDS = 30
HISTORY_RETENTION_SECONDS = 48 * 60 * 60
EMPTY_RESERVE_VOLTAGE = 3.45
EMPTY_RESERVE_MAX_VOLTAGE = 3.6
EMPTY_LOOKBACK_SECONDS = 3 * 60
EMPTY_REFERENCE_WINDOW_SECONDS = 2 * 60
MODEL_VERSION = 2


def current_polarity():
    value = int(os.getenv('INA219_CURRENT_POLARITY', '-1'))
    if value not in (-1, 1):
        raise ValueError('INA219_CURRENT_POLARITY 必须为 -1 或 1')
    return value


# Representative 4.2 V Li-ion/LiPo open-circuit curve, not a pack-specific fuel
# gauge. Endpoints are adapted only after the cycle conditions below are met.
OCV_CURVE = (
    (3.00, 0), (3.30, 2), (3.50, 5), (3.60, 10), (3.70, 20),
    (3.75, 30), (3.80, 40), (3.85, 50), (3.90, 60), (3.95, 70),
    (4.00, 80), (4.10, 90), (4.20, 100),
)


def _curve_level(voltage):
    for (left_v, left_p), (right_v, right_p) in zip(OCV_CURVE, OCV_CURVE[1:]):
        if voltage <= right_v:
            return left_p + (voltage - left_v) / (right_v - left_v) * (right_p - left_p)
    (left_v, left_p), (right_v, right_p) = OCV_CURVE[-2:]
    return right_p + (voltage - right_v) / (right_v - left_v) * (right_p - left_p)


def estimate_percent(voltage, empty_voltage=3.0, full_voltage=4.2):
    # Reserve capacity on the existing nonlinear curve. Moving the zero point
    # must not stretch the whole voltage axis and distort mid-charge readings.
    if voltage <= empty_voltage:
        return 0
    if voltage >= full_voltage:
        return 100
    empty, full = _curve_level(empty_voltage), _curve_level(full_voltage)
    return max(0, min(100, round((_curve_level(voltage) - empty) / (full - empty) * 100)))


def read_boot_id():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip() or None
    except OSError:
        return None


@dataclass
class Learning:
    model_version: int = MODEL_VERSION
    current_polarity: int = -1
    full_voltage: float = 4.2
    empty_voltage: float = EMPTY_RESERVE_VOLTAGE
    full_calibrated_at: float | None = None
    full_calibration_method: str | None = None
    empty_calibrated_at: float | None = None
    empty_calibration_method: str = 'reserve_floor'
    empty_reference: dict | None = None
    resistance_ohm: float | None = None
    charge_seen: bool = False
    charge_peak_ma: float = 0
    taper_seen: bool = False
    full: bool = False
    pending_empty: dict | None = None
    last_time: float | None = None


class BatteryMonitor:
    def __init__(self, store, driver, reader, clock=time.time, boot_id_reader=None):
        self.store, self.driver, self.reader, self.clock = store, driver, reader, clock
        self.lock = Lock()
        self.current_polarity = current_polarity()
        self.learning = Learning(current_polarity=self.current_polarity)
        reset_learning = False
        row = store.one('SELECT payload FROM battery_learning WHERE singleton=1')
        if row and driver != 'mock':
            try:
                saved = json.loads(row['payload'])
                if not isinstance(saved, dict):
                    raise ValueError('Invalid battery calibration data')
                # The old pending value was a minimum terminal-region sample.
                # It cannot serve as the earlier, reserve-preserving reference.
                saved.pop('depleted_voltage', None)
                if saved.get('model_version') != MODEL_VERSION or saved.get('current_polarity') != self.current_polarity:
                    # Earlier versions learned using the wrong physical current
                    # direction. Their endpoints, resistance and cycle flags
                    # cannot be reused under the corrected convention.
                    reset_learning = True
                    log.info('Relearning battery parameters after current polarity change')
                else:
                    self.learning = Learning(**saved)
                    item = self.learning
                    if not (4.05 <= item.full_voltage <= 4.3 and 2.8 <= item.empty_voltage <= EMPTY_RESERVE_MAX_VOLTAGE
                            and (item.resistance_ohm is None or 0.02 <= item.resistance_ohm <= 0.5)
                            and item.full_calibration_method in (None, 'charge_taper', 'stable_idle')
                            and item.empty_calibration_method in ('reserve_floor', 'pre_cutoff_window')
                            and (item.pending_empty is None or self._valid_empty_reference(item.pending_empty))):
                        raise ValueError('Invalid learned battery values')
            except (TypeError, ValueError):
                log.warning('Ignoring invalid battery calibration', exc_info=True)
                self.learning = Learning(current_polarity=self.current_polarity)
                reset_learning = True
        self.cached = None
        self.cached_at = None
        self.saved_at = self.clock()
        self.previous = None
        self.resistance_samples = []
        self.full_candidate = None
        self.filtered_ocv = None
        self.last_voltage = None
        self.session_id = None
        self.last_recorded_at = None
        self.last_recorded_phase = None
        self.last_recorded_low = None
        self.pending_samples = []
        self.last_flushed_at = None
        self.last_flushed_low = False
        self.learning_dirty = False
        self.last_history_cleanup = self.clock()
        if self.learning.empty_voltage < EMPTY_RESERVE_VOLTAGE:
            self.learning.empty_voltage = EMPTY_RESERVE_VOLTAGE
            self.learning.empty_calibrated_at = None
            self.learning.empty_calibration_method = 'reserve_floor'
            self.learning.empty_reference = None
        if driver != 'mock':
            boot_id = (boot_id_reader or read_boot_id)()
            self.session_id = new_id()
            with store.connection() as db:
                previous = db.execute('SELECT * FROM battery_sessions ORDER BY rowid DESC LIMIT 1').fetchone()
                reason = ('first_start' if previous is None else 'clean_restart' if previous['ended_at'] is not None else
                          'unclean_reboot' if boot_id and previous['boot_id'] and boot_id != previous['boot_id'] else
                          'service_restart')
                db.execute('INSERT INTO battery_sessions(id,boot_id,started_at,previous_session_id,restart_reason) VALUES(?,?,?,?,?)',
                           (self.session_id, boot_id, self.clock(), previous['id'] if previous else None, reason))
            if not reset_learning and reason == 'unclean_reboot':
                candidate = self._empty_reference(previous['id'], 3.4, reason)
                if candidate is not None and (self.learning.pending_empty is None or
                                               candidate['voltage'] > self.learning.pending_empty['voltage']):
                    self.learning.pending_empty = candidate
            self._save(self.clock())

    def _save(self, now):
        if self.driver == 'mock':
            return
        with self.store.connection() as db:
            db.execute('INSERT INTO battery_learning(singleton,payload) VALUES(1,?) '
                       'ON CONFLICT(singleton) DO UPDATE SET payload=excluded.payload',
                       (json.dumps(asdict(self.learning)),))
        self.saved_at = now
        self.learning_dirty = False

    @staticmethod
    def _valid_empty_reference(reference):
        try:
            return (isinstance(reference, dict)
                    and EMPTY_RESERVE_VOLTAGE <= reference['voltage'] <= EMPTY_RESERVE_MAX_VOLTAGE
                    and 3.2 <= reference['reference_voltage'] <= LOW_RECORD_VOLTAGE
                    and reference['sample_count'] >= 3 and reference['window_end'] - reference['window_start'] >= 8
                    and reference['window_end'] <= reference['last_sample_at'] - EMPTY_LOOKBACK_SECONDS
                    and bool(reference['source_session'])
                    and reference['reason'] in ('unclean_reboot', 'critical_discharge'))
        except (KeyError, TypeError):
            return False

    def _record_sample(self, now, voltage, ocv, current):
        if self.session_id is None:
            return
        low = voltage <= LOW_RECORD_VOLTAGE
        phase = ('unknown' if current is None else 'charging' if current < -CURRENT_DEADBAND_MA else
                 'discharging' if current > CURRENT_DEADBAND_MA else 'idle')
        interval = LOW_SAMPLE_SECONDS if low else NORMAL_RECORD_SECONDS
        transition = low != self.last_recorded_low or phase != self.last_recorded_phase
        if transition or self.last_recorded_at is None or not 0 <= now - self.last_recorded_at < interval:
            self.pending_samples.append((self.session_id, now, voltage, ocv, current))
            self.last_recorded_at = now
            self.last_recorded_phase = phase
            self.last_recorded_low = low
        self._flush_samples(now)

    def _flush_samples(self, now, force=False):
        if not self.pending_samples:
            return
        low = self.last_flushed_low or any(row[2] <= LOW_RECORD_VOLTAGE for row in self.pending_samples)
        interval = LOW_RECORD_SECONDS if low else NORMAL_RECORD_SECONDS
        if not force and self.last_flushed_at is not None and 0 <= now - self.last_flushed_at < interval:
            return
        with self.store.connection() as db:
            db.executemany('INSERT INTO battery_samples(session_id,recorded_at,voltage,ocv_voltage,current_ma) VALUES(?,?,?,?,?)',
                           self.pending_samples)
            db.execute('UPDATE battery_sessions SET last_seen_at=? WHERE id=?', (self.pending_samples[-1][1], self.session_id))
            if now - self.last_history_cleanup >= 60:
                cutoff = now - HISTORY_RETENTION_SECONDS
                db.execute('DELETE FROM battery_samples WHERE recorded_at<?', (cutoff,))
                db.execute('DELETE FROM battery_sessions WHERE id<>? AND COALESCE(last_seen_at,started_at)<?',
                           (self.session_id, cutoff))
                self.last_history_cleanup = now
        self.last_flushed_at = now
        self.last_flushed_low = self.pending_samples[-1][2] <= LOW_RECORD_VOLTAGE
        self.pending_samples.clear()

    def _empty_reference(self, session_id, terminal_limit, reason):
        with self.store.connection() as db:
            last = db.execute('SELECT * FROM battery_samples WHERE session_id=? ORDER BY id DESC LIMIT 1',
                              (session_id,)).fetchone()
            if (last is None or last['current_ma'] is None or last['current_ma'] <= CURRENT_DEADBAND_MA
                    or not 2.0 <= last['voltage'] <= terminal_limit or last['ocv_voltage'] > LOW_RECORD_VOLTAGE):
                return None
            recent = db.execute('SELECT recorded_at FROM battery_samples WHERE session_id=? AND recorded_at>=? '
                                'AND voltage<=? AND current_ma>? ORDER BY recorded_at',
                                (session_id, last['recorded_at'] - 10, LOW_RECORD_VOLTAGE, CURRENT_DEADBAND_MA)).fetchall()
            if len(recent) < 3 or recent[-1][0] - recent[0][0] < 2:
                return None
            end = last['recorded_at'] - EMPTY_LOOKBACK_SECONDS
            rows = db.execute('SELECT recorded_at,ocv_voltage FROM battery_samples WHERE session_id=? '
                              'AND recorded_at>=? AND recorded_at<=? AND current_ma>? ORDER BY recorded_at',
                              (session_id, end - EMPTY_REFERENCE_WINDOW_SECONDS, end, CURRENT_DEADBAND_MA)).fetchall()
        if (len(rows) < 3 or rows[-1][0] - rows[0][0] < 8
                or any(right[0] - left[0] > SAMPLE_SECONDS * 3 for left, right in zip(rows, rows[1:]))):
            return None
        levels = sorted(row[1] for row in rows)
        if levels[0] < 3.2 or levels[-1] > LOW_RECORD_VOLTAGE or levels[-1] - levels[0] > .08 + 1e-9:
            return None
        # Prefer the upper quartile, with a 20 mV reserve, rather than the final
        # collapse or a minimum. Never learn a smaller reserve than before.
        reference = levels[math.ceil((len(levels) - 1) * .75)]
        return {
            'voltage': round(min(EMPTY_RESERVE_MAX_VOLTAGE, max(EMPTY_RESERVE_VOLTAGE, reference + .02)), 3),
            'reference_voltage': round(reference, 3), 'window_start': rows[0][0], 'window_end': rows[-1][0],
            'sample_count': len(rows), 'source_session': session_id, 'last_sample_at': last['recorded_at'], 'reason': reason,
        }

    def _apply_empty_reference(self, now):
        item = self.learning
        if item.pending_empty is None:
            return False
        item.empty_voltage = max(item.empty_voltage, item.pending_empty['voltage'])
        item.empty_calibrated_at = now
        item.empty_calibration_method = 'pre_cutoff_window'
        item.empty_reference = item.pending_empty
        item.pending_empty = None
        log.info('Learned conservative empty battery voltage %.3f V', item.empty_voltage)
        return True

    def close(self):
        with self.lock:
            if self.session_id is not None:
                self._flush_samples(self.clock(), force=True)
                self._save(self.clock())
                with self.store.connection() as db:
                    db.execute('UPDATE battery_sessions SET ended_at=? WHERE id=?', (self.clock(), self.session_id))

    def status(self, force=False):
        with self.lock:
            now = self.clock()
            interval = LOW_SAMPLE_SECONDS if self.last_voltage is not None and self.last_voltage <= LOW_RECORD_VOLTAGE else SAMPLE_SECONDS
            if not force and self.cached_at is not None and 0 <= now - self.cached_at < interval:
                self._flush_samples(now)
                return self.cached.copy()
            raw = self.reader(self.driver)
            result = dict(raw)
            result.update(charging=None, charge_state='unknown', method='ocv_curve', full_confirmation=None,
                          calibration={key: getattr(self.learning, key) for key in (
                              'full_voltage', 'empty_voltage', 'full_calibrated_at', 'full_calibration_method',
                              'empty_calibrated_at', 'empty_calibration_method', 'empty_reference', 'pending_empty', 'resistance_ohm')})
            if not raw['available']:
                self._flush_samples(now)
                self.full_candidate = self.previous = None
            else:
                self.last_voltage = raw['voltage']
                self._update(result, now)
            self.cached, self.cached_at = result, now
            return result.copy()

    def _update(self, result, now):
        voltage = result['voltage']
        current = result.get('current_ma')
        item = self.learning
        previous_flags = (item.charge_seen, item.taper_seen, item.full, item.pending_empty)
        previous = self.previous
        continuous = previous is not None and 0 < now - previous[0] <= SAMPLE_SECONDS * 3
        if not continuous:
            self.full_candidate = None
        if current is not None and not math.isfinite(current):
            current = None

        # Learn cell/path resistance from discharge load steps, rather than
        # inventing a fixed resistance. Reject charging, long gaps and outliers.
        if continuous and current is not None and previous[2] is not None:
            old_current = previous[2]
            delta = (current - old_current) / 1000
            if current > CURRENT_DEADBAND_MA and old_current > CURRENT_DEADBAND_MA and abs(delta) >= 0.1:
                resistance = -(voltage - previous[1]) / delta
                if 0.02 <= resistance <= 0.5:
                    self.resistance_samples.append(resistance)
                    self.resistance_samples = self.resistance_samples[-9:]
                    if len(self.resistance_samples) >= 3:
                        item.resistance_ohm = round(median(self.resistance_samples), 4)

        ocv = voltage + (current or 0) / 1000 * (item.resistance_ohm or 0)
        self._record_sample(now, voltage, ocv, current)
        changed = False
        if current is None:
            self.full_candidate = None
        elif current < -CURRENT_DEADBAND_MA:
            result.update(charging=True, charge_state='charging')
            item.full = False
            changed = self._apply_empty_reference(now)
            item.charge_seen = True
            item.charge_peak_ma = max(item.charge_peak_ma, -current)
            if (item.charge_peak_ma >= 100 and -current <= max(50, item.charge_peak_ma * .2)
                    and 4.1 <= voltage <= 4.3):
                item.taper_seen = True
            self.full_candidate = None
        elif current > CURRENT_DEADBAND_MA:
            result.update(charging=False, charge_state='discharging')
            item.full = item.charge_seen = False
            item.taper_seen = False
            item.charge_peak_ma = 0
            self.full_candidate = None
            if voltage <= 3.15 and item.pending_empty is None:
                item.pending_empty = self._empty_reference(self.session_id, 3.15, 'critical_discharge')
            elif voltage > LOW_RECORD_VOLTAGE:
                item.pending_empty = None
        else:
            result.update(charging=False, charge_state='idle')
            if voltage > LOW_RECORD_VOLTAGE and not item.charge_seen and not 4.1 <= voltage <= 4.3:
                item.pending_empty = None
            # Normal charging evidence permits a three-minute confirmation.
            # Startup after charging or missed taper samples need a much longer
            # high-voltage, near-zero-current plateau. INA219 cannot read the
            # charger's termination indicator, so both paths remain estimates.
            if not item.full and 4.1 <= voltage <= 4.3:
                observed_taper = item.charge_seen and item.taper_seen
                required = FULL_SETTLE_SECONDS if observed_taper else FULL_RECOVERY_SETTLE_SECONDS
                method = 'charge_taper' if observed_taper else 'stable_idle'
                if self.full_candidate is None:
                    self.full_candidate = (now, voltage, voltage)
                since, low, high = self.full_candidate
                low, high = min(low, voltage), max(high, voltage)
                if high - low > 0.02 + 1e-9:
                    self.full_candidate = (now, voltage, voltage)
                else:
                    self.full_candidate = (since, low, high)
                    if now - since >= required:
                        item.full_voltage = round((low + high) / 2, 3)
                        item.full_calibrated_at = now
                        item.full_calibration_method = method
                        item.full, item.charge_seen = True, False
                        item.taper_seen = False
                        item.charge_peak_ma = 0
                        self.full_candidate = None
                        changed = True
                        changed |= self._apply_empty_reference(now)
                        log.info('Learned full battery voltage %.3f V via %s', item.full_voltage, method)
                if self.full_candidate is not None:
                    result['full_confirmation'] = {
                        'elapsed_seconds': int(now - self.full_candidate[0]),
                        'required_seconds': required, 'method': method,
                    }
            else:
                self.full_candidate = None
            if item.full and voltage >= item.full_voltage - 0.03:
                result['charge_state'] = 'full'
            else:
                item.full = False

        item.last_time = now
        if continuous and self.filtered_ocv is not None:
            alpha = 1 - math.exp(-(now - previous[0]) / 30)
            self.filtered_ocv += alpha * (ocv - self.filtered_ocv)
        else:
            self.filtered_ocv = ocv
        result['percent'] = (100 if result['charge_state'] == 'full' else 0 if ocv <= item.empty_voltage else
                             estimate_percent(self.filtered_ocv, item.empty_voltage, item.full_voltage))
        result['ocv_voltage'] = round(ocv, 3)
        result['low'] = result['percent'] < LOW_BATTERY_PERCENT
        result['calibration'] = {key: getattr(item, key) for key in result['calibration']}
        self.previous = (now, voltage, current)
        self.learning_dirty |= previous_flags != (item.charge_seen, item.taper_seen, item.full, item.pending_empty)
        if changed or now - self.saved_at >= NORMAL_RECORD_SECONDS or (self.learning_dirty and now - self.saved_at >= LOW_RECORD_SECONDS):
            self._save(now)
