"""Tests for metrics_poller filtering of non-real disks and interfaces.

Sample lines mirror what a Synology DSM Node Exporter actually exposes:
a /dev/loop0 service mount under /tmp and a sit0 tunnel interface.
"""

from metrics_poller import _parse, extract_metrics


DISK_SAMPLE = """
node_filesystem_size_bytes{device="/dev/md0",fstype="ext4",mountpoint="/"} 8388608000
node_filesystem_avail_bytes{device="/dev/md0",fstype="ext4",mountpoint="/"} 6979321856
node_filesystem_size_bytes{device="/dev/loop0",fstype="ext4",mountpoint="/tmp/SynologyAuthService"} 4194304
node_filesystem_avail_bytes{device="/dev/loop0",fstype="ext4",mountpoint="/tmp/SynologyAuthService"} 3728704
node_filesystem_size_bytes{device="/dev/vg1/volume_1",fstype="btrfs",mountpoint="/volume1"} 9589934592000
node_filesystem_avail_bytes{device="/dev/vg1/volume_1",fstype="btrfs",mountpoint="/volume1"} 9520000000000
"""

NET_SAMPLE = """
node_network_receive_bytes_total{device="eth0"} 1000000
node_network_transmit_bytes_total{device="eth0"} 500000
node_network_receive_bytes_total{device="sit0"} 0
node_network_transmit_bytes_total{device="sit0"} 0
node_network_receive_bytes_total{device="lo"} 12345
node_network_transmit_bytes_total{device="lo"} 12345
"""


def _mounts(text):
    return [d["mount"] for d in extract_metrics(_parse(text), None)["disks"]]


def test_loop_device_mount_excluded():
    assert "/tmp/SynologyAuthService" not in _mounts(DISK_SAMPLE)


def test_real_filesystems_still_included():
    mounts = _mounts(DISK_SAMPLE)
    assert mounts == ["/", "/volume1"]


def test_loop_mount_does_not_shadow_real_mount():
    """A loop device sharing a mountpoint must not consume the 'seen' slot."""
    text = """
node_filesystem_size_bytes{device="/dev/loop7",fstype="ext4",mountpoint="/volume1"} 4194304
node_filesystem_avail_bytes{device="/dev/loop7",fstype="ext4",mountpoint="/volume1"} 1048576
node_filesystem_size_bytes{device="/dev/vg1/volume_1",fstype="btrfs",mountpoint="/volume1"} 1000000000
node_filesystem_avail_bytes{device="/dev/vg1/volume_1",fstype="btrfs",mountpoint="/volume1"} 900000000
"""
    disks = extract_metrics(_parse(text), None)["disks"]
    assert len(disks) == 1
    assert disks[0]["device"] == "/dev/vg1/volume_1"


def test_sit_tunnel_interface_excluded():
    out = extract_metrics(_parse(NET_SAMPLE), None)
    assert "sit0" not in out["_net_raw"]


def test_real_interface_still_included():
    out = extract_metrics(_parse(NET_SAMPLE), None)
    assert set(out["_net_raw"]) == {"eth0"}
