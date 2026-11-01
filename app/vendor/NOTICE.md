# Waveshare driver attribution

`epd7in3e.py` is copied unchanged from Waveshare's product repository:
https://github.com/waveshareteam/RPi_Zero_PhotoPainter/blob/d47d28d715d9dd9ff9e40a9a1019b9568bcbb867/7in3_e-Paper_E/python/lib/waveshare_epd/epd7in3e.py

Upstream commit: d47d28d715d9dd9ff9e40a9a1019b9568bcbb867
The original permissive license and copyright notice are retained in that file.
`epdconfig.py` is this project's adapter, not the upstream platform detector.

Product pinout (BCM): RST 17, DC 25, BUSY 24, PWR 27; SPI0 CE0 via
/dev/spidev0.0. Hardware chip select is controlled by spidev, as in the
vendor RaspberryPi configuration. UPS I²C address 0x43 and the 3.0–4.2 V
single-cell operating range, 0.01 ohm shunt and 32-sample ADC configuration
are from UPS/INA219.py at the same upstream commit. Current is read directly
from signed shunt voltage (10 uV/LSB), without relying on the calibration
register. Charging direction is determined by the board wiring, not by the
upstream register sign: observed PhotoPainter raw current is negative on
battery power and positive while charging. The adapter normalizes current
to positive discharge / negative charge. Bus voltage is kept as measured.
The non-linear battery model and cycle learning are project code.
