"""Tests for metrics_poller's parser and extract_metrics computed fields.

Covers the single-sample computed values (system info, load, memory, swap,
cpu count, temperatures, uptime formatting) and the two-sample delta paths
(cpu_percent and network rx/tx rates) that depend on a previous sample.
"""

import math

from metrics_poller import _parse, extract_metrics


GB = 1024 ** 3

_MEM_TOTAL = 8 * GB
_MEM_AVAIL = 2 * GB
_SWAP_TOTAL = 2 * GB
_SWAP_FREE = 1 * GB

SAMPLE = f"""
node_uname_info{{nodename="pi5",release="6.6.0",machine="aarch64"}} 1
node_load1 0.10
node_load5 0.20
node_load15 0.30
node_memory_MemTotal_bytes {_MEM_TOTAL}
node_memory_MemAvailable_bytes {_MEM_AVAIL}
node_memory_SwapTotal_bytes {_SWAP_TOTAL}
node_memory_SwapFree_bytes {_SWAP_FREE}
node_cpu_seconds_total{{cpu="0",mode="idle"}} 1000
node_cpu_seconds_total{{cpu="0",mode="user"}} 200
node_cpu_seconds_total{{cpu="1",mode="idle"}} 1100
node_cpu_seconds_total{{cpu="1",mode="user"}} 300
node_cpu_seconds_total{{cpu="2",mode="idle"}} 900
node_cpu_seconds_total{{cpu="2",mode="user"}} 100
node_cpu_seconds_total{{cpu="3",mode="idle"}} 950
node_cpu_seconds_total{{cpu="3",mode="user"}} 150
node_thermal_zone_temp{{type="cpu-thermal"}} 45.0
node_thermal_zone_temp{{type="zone-cold"}} 0
node_hwmon_temp_celsius{{sensor="temp1_input",chip="cpu"}} 50.0
node_hwmon_temp_celsius{{sensor="temp1_max",chip="cpu"}} 90.0
"""


def test_parse_plain_labelled_and_timestamp_metrics():
    parsed = _parse("""
plain_metric 1.5
labelled_metric{label="value"} 2
timestamped_metric 3 1710000000000
""")

    assert parsed == {
        "plain_metric": [{"labels": {}, "value": 1.5}],
        "labelled_metric": [{"labels": {"label": "value"}, "value": 2.0}],
        "timestamped_metric": [{"labels": {}, "value": 3.0}],
    }


def test_parse_space_in_label_value_and_extract_disk():
    parsed = _parse("""
node_filesystem_size_bytes{device="/dev/sdb1",fstype="ext4",mountpoint="/media/My Drive"} 1.2e+11
node_filesystem_avail_bytes{device="/dev/sdb1",fstype="ext4",mountpoint="/media/My Drive"} 6e+10
""")

    assert parsed["node_filesystem_size_bytes"][0]["labels"]["mountpoint"] == "/media/My Drive"
    disks = extract_metrics(parsed, None)["disks"]
    assert [(disk["device"], disk["mount"]) for disk in disks] == [("/dev/sdb1", "/media/My Drive")]


def test_parse_unescapes_label_values():
    parsed = _parse(r'example{quote="a\"b",slash="a\\b",newline="a\nb"} 1')

    assert parsed["example"][0]["labels"] == {
        "quote": 'a"b',
        "slash": "a\\b",
        "newline": "a\nb",
    }


def test_parse_delimiters_inside_quoted_label_value():
    parsed = _parse(r'example{value="one,two{three}=four"} 1')

    assert parsed["example"][0]["labels"]["value"] == "one,two{three}=four"


def test_parse_accepts_nan_and_infinities():
    parsed = _parse("""
nan_metric NaN
positive_infinity +Inf
negative_infinity -Inf
""")

    assert math.isnan(parsed["nan_metric"][0]["value"])
    assert parsed["positive_infinity"][0]["value"] == math.inf
    assert parsed["negative_infinity"][0]["value"] == -math.inf


def test_parse_skips_malformed_lines_and_keeps_valid_neighbors():
    parsed = _parse("""
before 1
unterminated_quote{label="value} 2
unterminated_block{label="value" 3
missing_value{label="value"}
non_numeric{label="value"} nope
too_many_fields{label="value"} 1 2 3
after 4
""")

    assert parsed == {
        "before": [{"labels": {}, "value": 1.0}],
        "after": [{"labels": {}, "value": 4.0}],
    }


def test_system_info_from_uname_labels():
    out = extract_metrics(_parse(SAMPLE), None)
    assert out["hostname"] == "pi5"
    assert out["kernel"] == "6.6.0"
    assert out["arch"] == "aarch64"


def test_load_averages_rounded():
    out = extract_metrics(_parse(SAMPLE), None)
    assert out["load1"] == 0.1
    assert out["load5"] == 0.2
    assert out["load15"] == 0.3


def test_memory_totals_and_percent():
    out = extract_metrics(_parse(SAMPLE), None)
    assert out["ram_total_gb"] == 8.0
    assert out["ram_used_gb"] == 6.0
    assert out["ram_percent"] == 75.0


def test_swap_totals_and_percent():
    out = extract_metrics(_parse(SAMPLE), None)
    assert out["swap_total_gb"] == 2.0
    assert out["swap_used_gb"] == 1.0
    assert out["swap_percent"] == 50.0


def test_swap_absent_when_swap_total_zero():
    text = SAMPLE.replace(
        f"node_memory_SwapTotal_bytes {_SWAP_TOTAL}",
        "node_memory_SwapTotal_bytes 0",
    )
    out = extract_metrics(_parse(text), None)
    assert "swap_total_gb" not in out
    assert "swap_used_gb" not in out
    assert "swap_percent" not in out


def test_cpu_count_from_distinct_cpu_labels():
    out = extract_metrics(_parse(SAMPLE), None)
    assert out["cpu_count"] == 4


def test_temperatures_normal_zero_and_max_sensor():
    out = extract_metrics(_parse(SAMPLE), None)
    labels = {t["label"]: t["celsius"] for t in out["temps"]}
    assert labels.get("cpu-thermal") == 45.0
    assert labels.get("temp1_input") == 50.0
    assert "zone-cold" not in labels
    assert "temp1_max" not in labels


def test_uptime_days_hours_minutes(monkeypatch):
    fixed = 1_700_000_000.0
    monkeypatch.setattr("metrics_poller.time.time", lambda: fixed)
    elapsed = 2 * 86400 + 3 * 3600 + 4 * 60
    text = f"node_boot_time_seconds {fixed - elapsed}"
    out = extract_metrics(_parse(text), None)
    assert out["uptime"] == "2d 3h 4m"


def test_uptime_hours_minutes(monkeypatch):
    fixed = 1_700_000_000.0
    monkeypatch.setattr("metrics_poller.time.time", lambda: fixed)
    elapsed = 5 * 3600 + 6 * 60
    text = f"node_boot_time_seconds {fixed - elapsed}"
    out = extract_metrics(_parse(text), None)
    assert out["uptime"] == "5h 6m"


def test_uptime_minutes_only(monkeypatch):
    fixed = 1_700_000_000.0
    monkeypatch.setattr("metrics_poller.time.time", lambda: fixed)
    elapsed = 7 * 60
    text = f"node_boot_time_seconds {fixed - elapsed}"
    out = extract_metrics(_parse(text), None)
    assert out["uptime"] == "7m"


def test_cpu_percent_from_two_samples():
    sample1 = """
node_cpu_seconds_total{cpu="0",mode="idle"} 1000
node_cpu_seconds_total{cpu="0",mode="user"} 200
node_cpu_seconds_total{cpu="1",mode="idle"} 1100
node_cpu_seconds_total{cpu="1",mode="user"} 300
"""
    sample2 = """
node_cpu_seconds_total{cpu="0",mode="idle"} 1050
node_cpu_seconds_total{cpu="0",mode="user"} 250
node_cpu_seconds_total{cpu="1",mode="idle"} 1150
node_cpu_seconds_total{cpu="1",mode="user"} 350
"""
    prev = extract_metrics(_parse(sample1), None)
    out = extract_metrics(_parse(sample2), prev)

    # total delta = 200, idle delta = 100
    assert out["cpu_percent"] == round((1 - 100 / 200) * 100, 1)
    assert out["cpu_percent"] == 50.0


def test_network_rates_from_two_samples(monkeypatch):
    times = iter([1000.0, 1010.0])
    monkeypatch.setattr("metrics_poller.time.time", lambda: next(times))

    sample1 = """
node_network_receive_bytes_total{device="eth0"} 1000000
node_network_transmit_bytes_total{device="eth0"} 500000
"""
    rx_delta = 102400
    tx_delta = 51200
    sample2 = f"""
node_network_receive_bytes_total{{device="eth0"}} {1000000 + rx_delta}
node_network_transmit_bytes_total{{device="eth0"}} {500000 + tx_delta}
"""
    prev = extract_metrics(_parse(sample1), None)
    out = extract_metrics(_parse(sample2), prev)

    elapsed = 10.0
    assert out["network"]["eth0"]["rx_kbs"] == round(rx_delta / elapsed / 1024, 1)
    assert out["network"]["eth0"]["tx_kbs"] == round(tx_delta / elapsed / 1024, 1)
    assert out["network"]["eth0"]["rx_kbs"] == 10.0
    assert out["network"]["eth0"]["tx_kbs"] == 5.0
