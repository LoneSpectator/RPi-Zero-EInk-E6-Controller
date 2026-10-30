"""Product GPIO adapter, using kernel SPI CS and lgpio (no board auto-detection)."""
import os
import time

RST_PIN, DC_PIN, CS_PIN, BUSY_PIN, PWR_PIN = 17, 25, 8, 24, 27
_spi = None
_chip = None
_gpio = None


def module_init():
    global _spi, _chip, _gpio
    import lgpio
    import spidev
    _gpio = lgpio
    try:
        _chip = lgpio.gpiochip_open(int(os.getenv('GPIO_CHIP', '0')))
        for pin in (RST_PIN, DC_PIN, PWR_PIN):
            lgpio.gpio_claim_output(_chip, pin, 0)
        lgpio.gpio_claim_input(_chip, BUSY_PIN)
        lgpio.gpio_write(_chip, PWR_PIN, 1)
        _spi = spidev.SpiDev()
        _spi.open(0, 0)
        _spi.max_speed_hz = 4_000_000
        _spi.mode = 0
        time.sleep(.1)
        return 0
    except Exception:
        module_exit()
        raise


def digital_write(pin, value):
    # /dev/spidev0.0 manages chip select, as in the vendor RaspberryPi adapter.
    if pin != CS_PIN:
        _gpio.gpio_write(_chip, pin, value)


def digital_read(pin):
    return _gpio.gpio_read(_chip, pin)


def delay_ms(ms):
    time.sleep(ms / 1000)


def spi_writebyte(data):
    _spi.writebytes(data)


def spi_writebyte2(data):
    _spi.writebytes2(data)


def module_exit():
    global _spi, _chip
    if _spi is not None:
        _spi.close()
        _spi = None
    if _chip is not None:
        for pin in (RST_PIN, DC_PIN, PWR_PIN):
            try:
                _gpio.gpio_write(_chip, pin, 0)
            except Exception:
                pass  # May not have been claimed if module_init failed.
        _gpio.gpiochip_close(_chip)
        _chip = None
