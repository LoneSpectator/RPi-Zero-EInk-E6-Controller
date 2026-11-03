"""Single-cell Li-ion voltage model and persistent, conservative cycle learning."""
from dataclasses import asdict, dataclass
import json
import logging
import math
import os
from statistics import median
from threading import Lock
import time


log = logging.getLogger(__name__)
LOW_BATTERY_PERCENT = 20
CURRENT_DEADBAND_MA = 10
SAMPLE_SECONDS = 4
FULL_SETTLE_SECONDS = 180
FULL_RECOVERY_SETTLE_SECONDS = 15 * 60
EMPTY_SETTLE_SECONDS = 30
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


def estimate_percent(voltage, empty_voltage=3.0, full_voltage=4.2):
    # Re-map learned endpoints onto the reference curve before interpolation.
    reference = 3.0 + (voltage - empty_voltage) / (full_voltage - empty_voltage) * 1.2
    if reference <= OCV_CURVE[0][0]:
        return 0
    for (left_v, left_p), (right_v, right_p) in zip(OCV_CURVE, OCV_CURVE[1:]):
        if reference <= right_v:
            return round(left_p + (reference - left_v) / (right_v - left_v) * (right_p - left_p))
    return 100


@dataclass
class Learning:
    model_version: int = MODEL_VERSION
    current_polarity: int = -1
    full_voltage: float = 4.2
    empty_voltage: float = 3.0
    full_calibrated_at: float | None = None
    full_calibration_method: str | None = None
    empty_calibrated_at: float | None = None
    resistance_ohm: float | None = None
    charge_seen: bool = False
    charge_peak_ma: float = 0
    taper_seen: bool = False
    full: bool = False
    depleted_voltage: float | None = None
    last_time: float | None = None


class BatteryMonitor:
    def __init__(self, store, driver, reader, clock=time.time):
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
                if saved.get('model_version') != MODEL_VERSION or saved.get('current_polarity') != self.current_polarity:
                    # Earlier versions learned using the wrong physical current
                    # direction. Their endpoints, resistance and cycle flags
                    # cannot be reused under the corrected convention.
                    reset_learning = True
                    log.info('Relearning battery parameters after current polarity change')
                else:
                    self.learning = Learning(**saved)
                    item = self.learning
                    if not (4.05 <= item.full_voltage <= 4.3 and 2.8 <= item.empty_voltage <= 3.4
                            and (item.resistance_ohm is None or 0.02 <= item.resistance_ohm <= 0.5)
                            and item.full_calibration_method in (None, 'charge_taper', 'stable_idle')):
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
        self.empty_candidate = None
        self.filtered_ocv = None
        if reset_learning:
            self._save(self.clock())

    def _save(self, now):
        if self.driver == 'mock':
            return
        with self.store.connection() as db:
            db.execute('INSERT INTO battery_learning(singleton,payload) VALUES(1,?) '
                       'ON CONFLICT(singleton) DO UPDATE SET payload=excluded.payload',
                       (json.dumps(asdict(self.learning)),))
        self.saved_at = now

    def status(self, force=False):
        with self.lock:
            now = self.clock()
            if not force and self.cached_at is not None and 0 <= now - self.cached_at < SAMPLE_SECONDS:
                return self.cached.copy()
            raw = self.reader(self.driver)
            result = dict(raw)
            result.update(charging=None, charge_state='unknown', method='ocv_curve', full_confirmation=None,
                          calibration={key: getattr(self.learning, key) for key in (
                              'full_voltage', 'empty_voltage', 'full_calibrated_at', 'full_calibration_method',
                              'empty_calibrated_at', 'resistance_ohm')})
            if not raw['available']:
                self.full_candidate = self.empty_candidate = self.previous = None
            else:
                self._update(result, now)
            self.cached, self.cached_at = result, now
            return result.copy()

    def _update(self, result, now):
        voltage = result['voltage']
        current = result.get('current_ma')
        item = self.learning
        previous_flags = (item.charge_seen, item.taper_seen, item.full, item.depleted_voltage)
        previous = self.previous
        continuous = previous is not None and 0 < now - previous[0] <= SAMPLE_SECONDS * 3
        if not continuous:
            self.full_candidate = self.empty_candidate = None
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
        changed = False
        if current is None:
            self.full_candidate = self.empty_candidate = None
        elif current < -CURRENT_DEADBAND_MA:
            result.update(charging=True, charge_state='charging')
            item.full = False
            # A sustained low-voltage discharge must have been observed before
            # starting a new charge. A brief voltage dip is not an empty cycle.
            if item.depleted_voltage is not None:
                item.empty_voltage = round(item.depleted_voltage, 3)
                item.empty_calibrated_at = now
                item.depleted_voltage = None
                changed = True
            item.charge_seen = True
            item.charge_peak_ma = max(item.charge_peak_ma, -current)
            if (item.charge_peak_ma >= 100 and -current <= max(50, item.charge_peak_ma * .2)
                    and 4.1 <= voltage <= 4.3):
                item.taper_seen = True
            self.full_candidate = self.empty_candidate = None
        elif current > CURRENT_DEADBAND_MA:
            result.update(charging=False, charge_state='discharging')
            item.full = item.charge_seen = False
            item.taper_seen = False
            item.charge_peak_ma = 0
            self.full_candidate = None
            if voltage <= 3.15 and 2.8 <= ocv <= 3.4:
                if self.empty_candidate is None:
                    self.empty_candidate = (now, ocv)
                since, minimum = self.empty_candidate
                minimum = min(minimum, ocv)
                self.empty_candidate = (since, minimum)
                if now - since >= EMPTY_SETTLE_SECONDS:
                    item.depleted_voltage = minimum
                    changed = True
            else:
                self.empty_candidate = None
                # Recovery above the depleted region means the earlier dip was
                # not a completed discharge (for example a temporary high load).
                if voltage > 3.4:
                    item.depleted_voltage = None
        else:
            result.update(charging=False, charge_state='idle')
            self.empty_candidate = None
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
        result['percent'] = (100 if result['charge_state'] == 'full' else
                             estimate_percent(self.filtered_ocv, item.empty_voltage, item.full_voltage))
        result['ocv_voltage'] = round(ocv, 3)
        result['low'] = result['percent'] < LOW_BATTERY_PERCENT
        result['calibration'] = {key: getattr(item, key) for key in result['calibration']}
        self.previous = (now, voltage, current)
        # Persist low-discharge evidence promptly so it survives battery cutoff.
        if (changed or previous_flags != (item.charge_seen, item.taper_seen, item.full, item.depleted_voltage)
                or now - self.saved_at >= 30 or (current is not None and current > 10 and voltage <= 3.2)):
            self._save(now)
