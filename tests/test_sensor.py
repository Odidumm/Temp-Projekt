"""DS18B20-Auswertung: fehlerhafte Werte werden verworfen."""

from __future__ import annotations

import pytest

from temp_monitor.sensor import DS18B20Sensor, FakeSensor, FileFakeSensor, SensorError, parse_w1_slave

GOOD = "72 01 4b 46 7f ff 0e 10 57 : crc=57 YES\n72 01 4b 46 7f ff 0e 10 57 t=23125\n"


def test_parse_valid():
    assert parse_w1_slave(GOOD) == pytest.approx(23.125)


def test_parse_negative():
    text = "5e ff 4b 46 7f ff 02 10 9b : crc=9b YES\n5e ff 4b 46 7f ff 02 10 9b t=-10125\n"
    assert parse_w1_slave(text) == pytest.approx(-10.125)


def test_parse_accepts_server_limits():
    line = "7f fd 4b 46 7f ff 01 10 aa"
    for raw in (-40000, 125000):
        assert parse_w1_slave(f"{line} : crc=aa YES\n{line} t={raw}\n") == raw / 1000


@pytest.mark.parametrize(
    "text, reason",
    [
        ("72 01 4b 46 7f ff 0e 10 57 : crc=12 NO\n72 01 4b 46 7f ff 0e 10 57 t=23125\n", "CRC"),
        ("50 05 4b 46 7f ff 0c 10 1c : crc=1c YES\n50 05 4b 46 7f ff 0c 10 1c t=85000\n", "85"),
        ("00 00 00 00 00 00 00 00 00 : crc=00 YES\n00 00 00 00 00 00 00 00 00 t=0\n", "Nullen"),
        ("ff ff ff ff ff ff ff ff ff : crc=c9 NO\nff ff ff ff ff ff ff ff ff t=-62\n", "CRC"),
        ("72 01 4b 46 7f ff 0e 10 57 : crc=57 YES\n72 01 4b 46 7f ff 0e 10 57\n", "Kein"),
        ("72 01 4b 46 7f ff 0e 10 57 : crc=57 YES\n72 01 4b 46 7f ff 0e 10 57 t=abc\n", "nicht lesbar"),
        ("72 01 4b 46 7f ff 0e 10 57 : crc=57 YES\n72 01 4b 46 7f ff 0e 10 57 t=127937\n", "Messbereich"),
        # unter -40 °C nimmt der Server nichts an (der Sensor könnte bis -55)
        ("7f fd 4b 46 7f ff 01 10 aa : crc=aa YES\n7f fd 4b 46 7f ff 01 10 aa t=-40062\n", "Messbereich"),
        ("72 01 4b 46 7f ff 0e 10 57 : crc=57 YES\n", "Unvollständig"),
        ("", "Unvollständig"),
    ],
)
def test_parse_rejects_bad_values(text, reason):
    with pytest.raises(SensorError, match=reason):
        parse_w1_slave(text)


def _make_device(base, name, content):
    device = base / name
    device.mkdir(parents=True)
    (device / "w1_slave").write_text(content)
    return device


def test_ds18b20_autodetects_first_28_device(tmp_path):
    _make_device(tmp_path, "00-620000000000", "Müll")  # Phantom-Gerät wird ignoriert
    _make_device(tmp_path, "28-5a097e0f64ff", GOOD)
    (tmp_path / "w1_bus_master1").mkdir()
    assert DS18B20Sensor(base_dir=tmp_path).read_celsius() == pytest.approx(23.125)


def test_ds18b20_explicit_device_id(tmp_path):
    _make_device(tmp_path, "28-000000000001", GOOD.replace("23125", "20000"))
    _make_device(tmp_path, "28-000000000002", GOOD)
    sensor = DS18B20Sensor(device_id="28-000000000002", base_dir=tmp_path)
    assert sensor.read_celsius() == pytest.approx(23.125)


def test_ds18b20_missing_sensor(tmp_path):
    with pytest.raises(SensorError, match="Kein DS18B20"):
        DS18B20Sensor(base_dir=tmp_path).read_celsius()
    with pytest.raises(SensorError, match="nicht lesbar"):
        DS18B20Sensor(device_id="28-weg", base_dir=tmp_path, attempts=1).read_celsius()


class _FlakyFile:
    """Liefert bei jedem Lesen die nächste Ausgabe (wie ein wackeliger Sensor)."""

    def __init__(self, *outputs):
        self.outputs = iter(outputs)

    def read_text(self, **_kw):
        return next(self.outputs)


BAD_CRC = GOOD.replace("crc=57 YES", "crc=00 NO")


def test_ds18b20_retries_once(tmp_path):
    sensor = DS18B20Sensor(base_dir=tmp_path, attempts=2, retry_delay_s=0)
    flaky = _FlakyFile(BAD_CRC, GOOD)
    sensor.device_file = lambda: flaky
    assert sensor.read_celsius() == pytest.approx(23.125)


def test_ds18b20_gives_up_after_attempts(tmp_path):
    sensor = DS18B20Sensor(base_dir=tmp_path, attempts=2, retry_delay_s=0)
    flaky = _FlakyFile(BAD_CRC, BAD_CRC, GOOD)
    sensor.device_file = lambda: flaky
    with pytest.raises(SensorError, match="CRC"):
        sensor.read_celsius()


def test_fake_sensor_sequence():
    sensor = FakeSensor(20.0, values=[21.0, SensorError("x"), 22.5])
    assert sensor.read_celsius() == 21.0
    with pytest.raises(SensorError):
        sensor.read_celsius()
    assert sensor.read_celsius() == 22.5
    assert sensor.read_celsius() == 22.5  # letzter Wert bleibt stehen


def test_file_fake_sensor(tmp_path):
    path = tmp_path / "temp"
    sensor = FileFakeSensor(path)
    with pytest.raises(SensorError):
        sensor.read_celsius()
    path.write_text("31.5\n")
    assert sensor.read_celsius() == 31.5
    path.write_text("heiß")
    with pytest.raises(SensorError):
        sensor.read_celsius()
