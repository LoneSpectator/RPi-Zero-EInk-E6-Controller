import os
import time


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
                'message': '模拟模式，无电池遥测'}
    try:
        from smbus2 import SMBus
        with SMBus(int(os.getenv('I2C_BUS', '1'))) as bus:
            # Product UPS example: 0x43; INA219 bus voltage register is big-endian.
            addr = int(os.getenv('INA219_ADDRESS', '0x43'), 0)
            config = bus.read_i2c_block_data(addr, 0, 2)
            # Preserve gain/range, ensure continuous bus+shunt measurement.
            value = ((config[0] << 8 | config[1]) & ~7) | 7
            bus.write_i2c_block_data(addr, 0, [value >> 8, value & 255])
            time.sleep(.02)
            raw = bus.read_i2c_block_data(addr, 2, 2)
            voltage = ((raw[0] << 8 | raw[1]) >> 3) * .004
        if not 2.5 <= voltage <= 4.5:
            raise ValueError(f'电池电压 {voltage:.3f} V 超出单节锂电合理范围，请检查地址和接线')
        return {'available': True, 'voltage': round(voltage, 3),
                'percent': round(min(100, max(0, (voltage - 3) / 1.2 * 100))),
                'estimated': True, 'message': '依据电压估算，非精密电量计'}
    except Exception as exc:
        return {'available': False, 'percent': None, 'voltage': None, 'estimated': True,
                'message': f'电池遥测不可用：{exc}'}
