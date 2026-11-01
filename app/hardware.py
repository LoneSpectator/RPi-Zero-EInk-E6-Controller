import os
import time
from .battery import estimate_percent, current_polarity


class MockDisplay:
    def __init__(self, delay=2):
        self.delay = delay

    def display(self, image):
        time.sleep(self.delay)


class WaveshareDisplay:
    def display(self, image):
        from .vendor import epd7in3e, epdconfig

        overall_deadline = time.monotonic() + 240

        class TimedEPD(epd7in3e.EPD):
            def ReadBusyH(self):
                deadline = min(overall_deadline, time.monotonic() + float(os.getenv('EPD_BUSY_TIMEOUT', '180')))
                while epdconfig.digital_read(self.busy_pin) == 0:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('电子纸 BUSY 等待超时，请检查供电、排线和 GPIO')
                    time.sleep(.01)

        screen = TimedEPD()
        try:
            if screen.init() != 0:
                raise RuntimeError('电子纸初始化失败')
            screen.display(screen.getbuffer(image))
            screen.sleep()
        finally:
            epdconfig.module_exit()


def battery_status(driver):
    if driver == 'mock':
        return {'available': False, 'percent': None, 'voltage': None, 'estimated': True,
                'current_ma': None,
                'message': '模拟模式，无电池遥测'}
    try:
        polarity = current_polarity()
        from smbus2 import SMBus
        with SMBus(int(os.getenv('I2C_BUS', '1'))) as bus:
            # Product UPS example: 0x43; INA219 bus voltage register is big-endian.
            addr = int(os.getenv('INA219_ADDRESS', '0x43'), 0)
            config = bus.read_i2c_block_data(addr, 0, 2)
            # Official UPS configuration: 16 V, +/-80 mV shunt, 32-sample
            # averaging for bus and shunt, continuous conversion (~34 ms).
            if (config[0] << 8 | config[1]) != 0x0EEF:
                bus.write_i2c_block_data(addr, 0, [0x0E, 0xEF])
                time.sleep(.04)
            raw = bus.read_i2c_block_data(addr, 2, 2)
            voltage = ((raw[0] << 8 | raw[1]) >> 3) * .004
            current = None
            raw_current = None
            current_error = ''
            try:
                raw = bus.read_i2c_block_data(addr, 1, 2)
                if len(raw) != 2:
                    raise ValueError('分流电压读数不完整')
                shunt = int.from_bytes(bytes(raw), 'big', signed=True)
                if abs(shunt) >= 8000:
                    raise ValueError('分流电压超出测量范围')
                # 10 uV/LSB, official board 0.01 ohm shunt => 1 mA/LSB.
                # Physical sign depends on board wiring. PhotoPainter readings
                # are negative on battery power, positive while charging.
                # The model consistently uses positive discharge / negative charge.
                raw_current = float(shunt)
                current = polarity * raw_current
            except Exception as exc:
                current_error = f'充电状态不可用：{exc}'
        if not 2.5 <= voltage <= 4.5:
            raise ValueError(f'电池电压 {voltage:.3f} V 超出单节锂电合理范围，请检查地址和接线')
        return {'available': True, 'voltage': round(voltage, 3),
                'current_ma': current, 'current_message': current_error,
                'raw_current_ma': raw_current, 'current_polarity': polarity,
                'percent': estimate_percent(voltage),
                'estimated': True, 'message': ''}
    except Exception as exc:
        return {'available': False, 'percent': None, 'voltage': None, 'estimated': True,
                'current_ma': None,
                'message': f'电池遥测不可用：{exc}'}
