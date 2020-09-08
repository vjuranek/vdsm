#
# Copyright 2017 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301  USA
#
# Refer to the README and COPYING files for full details of the license
#

from __future__ import absolute_import
from __future__ import division

import collections
import logging
import os
import uuid

import pytest

from vdsm.storage import lvmfilter
from vdsm.storage.lvmfilter import MountInfo

from . marks import requires_root

TEST_DIR = os.path.dirname(__file__)
FAKE_LSBLK = os.path.join(TEST_DIR, "fake-lsblk")
FAKE_DEVICES = ("/dev/disk/by-id/lvm-pv-uuid-FAKE-UUID",)

log = logging.getLogger("test")


FakeDevice = collections.namedtuple(
    "FakeDevice", "device, stable_link, unstable_link")


@pytest.fixture
def fake_device(tmpdir):
    """
    Creates tmp file as a fake partition and create a stable links to it,
    simulating /dev/aaa and /dev/disk/by-id/lvm-pv-uuid-bbb.
    """
    stable_name = "lvm-pv-uuid-{}".format(str(uuid.uuid4()))
    stable_link = str(tmpdir.join(stable_name))
    device = str(tmpdir.join("sda1"))

    open(device, "w").close()
    os.symlink(device, stable_link)

    return FakeDevice(device, stable_link, None)


@pytest.fixture
def fake_dm_device(tmpdir):
    """
    Creates tmp file as a fake device manged by device mapper and create a
    stable links to it as well as unstable one, simulating /dev/dm-a
    with links /dev/disk/by-id/lvm-pv-uuid-bbb and /dev/mapper/ccc.
    """
    stable_name = "lvm-pv-uuid-{}".format(str(uuid.uuid4()))
    stable_link = str(tmpdir.join(stable_name))
    unstable_link = str(tmpdir.join("mapped-device"))
    device = str(tmpdir.join("dm-1"))

    open(device, "w").close()
    os.symlink(device, stable_link)
    os.symlink(device, unstable_link)

    return FakeDevice(device, stable_link, unstable_link)


@pytest.mark.parametrize("plat,expected", [
    ("rhel74", [
        MountInfo("/dev/mapper/vg0-lv_home", "/home", FAKE_DEVICES),
        MountInfo("/dev/mapper/vg0-lv_root", "/", FAKE_DEVICES),
        MountInfo("/dev/mapper/vg0-lv_swap", "[SWAP]", FAKE_DEVICES),
    ]),
    ("fedora", [
        MountInfo("/dev/mapper/fedora-home", "/home", FAKE_DEVICES),
        MountInfo("/dev/mapper/fedora-root", "/", FAKE_DEVICES),
        MountInfo("/dev/mapper/fedora-swap", "[SWAP]", FAKE_DEVICES),
    ]),
])
def test_find_lvm_mounts(monkeypatch, plat, expected):
    # Monkeypatch the module to run the fake-lsblk returning data collected on
    # on real platform.
    monkeypatch.setattr(lvmfilter, "LSBLK", FAKE_LSBLK)
    monkeypatch.setenv("FAKE_STDOUT", FAKE_LSBLK + "." + plat + ".out")

    # Monkeypatch lvm helper, requires real devices on the host. We are testing
    # the helpers in other tests when running as root.

    def fake_vg_info(lv_path):
        if lv_path.endswith("-master"):
            return "vg_name", ["tag", lvmfilter.OVIRT_VG_TAG, "another"]
        else:
            return "vg_name", ["no,ovirt,tag"]

    monkeypatch.setattr(lvmfilter, "vg_info", fake_vg_info)
    monkeypatch.setattr(lvmfilter, "vg_devices", lambda x: FAKE_DEVICES)

    mounts = lvmfilter.find_lvm_mounts()
    log.info("found mounts %s", mounts)
    assert mounts == expected


def test_build_filter():
    mounts = [
        MountInfo("/dev/mapper/vg0-lv_home",
                  "/home",
                  ["/dev/sda2", "/dev/sdb2"]),
        MountInfo("/dev/mapper/vg0-lv_root",
                  "/",
                  ["/dev/sda2"]),
        MountInfo("/dev/mapper/vg0-lv_swap",
                  "[SWAP]",
                  ["/dev/sda2"]),
    ]
    lvm_filter = lvmfilter.build_filter(mounts)
    assert lvm_filter == ["a|^/dev/sda2$|", "a|^/dev/sdb2$|", "r|.*|"]


def test_build_filter_no_mounts():
    lvm_filter = lvmfilter.build_filter([])
    assert lvm_filter == ["r|.*|"]


def test_format_option():
    lvm_filter = ["a|^/dev/sda2$|", "r|.*|"]
    expected = 'filter = [ "a|^/dev/sda2$|", "r|.*|" ]'
    assert lvmfilter.format_option(lvm_filter) == expected


@requires_root
def test_real_find_lvm_mounts():
    mounts = lvmfilter.find_lvm_mounts()
    # This will return different results on any host, but we expect to find a
    # logical volume mounted at / with non empty devices list.
    for mnt in mounts:
        if mnt.mountpoint == "/":
            assert mnt.devices != []


@requires_root
def test_real_build_filter():
    mounts = lvmfilter.find_lvm_mounts()
    lvm_filter = lvmfilter.build_filter(mounts)
    log.info("LVM filter for this host:\n%r", lvm_filter)
    for mnt in mounts:
        for dev in mnt.devices:
            match = "a|^%s$|" % dev
            assert match in lvm_filter


def test_analyze_no_filter():
    # Trivial case: host does not have any filter.
    wanted_filter = ["a|^/dev/sda2$|", "r|.*|"]
    current_filter = None
    advice = lvmfilter.analyze(current_filter, wanted_filter)
    assert advice.action == lvmfilter.CONFIGURE
    assert advice.filter == wanted_filter


def test_analyze_configured():
    # Trivial case: host was already configured, no action needed.
    current_filter = wanted_filter = ["a|^/dev/sda2$|", "r|.*|"]
    advice = lvmfilter.analyze(current_filter, wanted_filter)
    assert advice.action == lvmfilter.UNNEEDED
    assert advice.filter is None


def test_analyze_different_order():
    # Same filter, order of devices does not matter.
    wanted_filter = ["a|^/dev/sda2$|", "a|^/dev/sdb2$|", "r|.*|"]
    current_filter = ["a|^/dev/sdb2$|", "a|^/dev/sda2$|", "r|.*|"]
    advice = lvmfilter.analyze(current_filter, wanted_filter)
    assert advice.action == lvmfilter.UNNEEDED
    assert advice.filter is None


def test_analyze_no_anchorces():
    # Curent filter uses non-strict regex witout anchores. This should work in
    # general, but we like to have a more strict filter.
    wanted_filter = ["a|^/dev/sda2$|", "r|.*|"]
    current_filter = ["a|/dev/sda2|", "r|.*|"]
    advice = lvmfilter.analyze(current_filter, wanted_filter)
    assert advice.action == lvmfilter.RECOMMEND
    assert advice.filter == wanted_filter


def test_analyze_missing_device():
    # Current filter is missing a device. Probably a user error, but the user
    # will have to resolve this.
    wanted_filter = ["a|^/dev/sda2$|", "a|^/dev/sdb2$|", "r|.*|"]
    current_filter = ["a|^/dev/sda2$|", "r|.*|"]
    advice = lvmfilter.analyze(current_filter, wanted_filter)
    assert advice.action == lvmfilter.RECOMMEND
    assert advice.filter == wanted_filter


def test_analyze_unknown_device():
    # Current filter includes an unknown device. This may be a user error,
    # removing a device without updating the filter, or maybe the user knows
    # better.
    wanted_filter = ["a|^/dev/sda2$|", "r|.*|"]
    current_filter = ["a|^/dev/sda2$|", "a|^/dev/sdb2$|", "r|.*|"]
    advice = lvmfilter.analyze(current_filter, wanted_filter)
    assert advice.action == lvmfilter.RECOMMEND
    assert advice.filter == wanted_filter


def test_analyze_extra_reject():
    # User wants to reject another device - does not make sense, but the user
    # knows better.
    wanted_filter = ["a|^/dev/sda2$|", "r|.*|"]
    current_filter = ["a|^/dev/sda2$|", "r|.*|", "r|/dev/foo|"]
    advice = lvmfilter.analyze(current_filter, wanted_filter)
    assert advice.action == lvmfilter.RECOMMEND
    assert advice.filter == wanted_filter


def test_analyze_invalid_filter_no_action():
    # Current filter is invalid - since LVM will reject this filter anyway, we
    # can configure a correct filter.
    wanted_filter = ["a|^/dev/sda2$|", "r|.*|"]
    current_filter = ["invalid", "filter"]
    with pytest.raises(lvmfilter.InvalidFilter):
        lvmfilter.analyze(current_filter, wanted_filter)


def test_analyze_invalid_filter_no_delimeter():
    # Current filter is invalid - since LVM will reject this filter anyway, we
    # can configure a correct filter.
    wanted_filter = ["a|^/dev/sda2$|", "r|.*|"]
    current_filter = ["a|invalid", "r|filter/"]
    with pytest.raises(lvmfilter.InvalidFilter):
        lvmfilter.analyze(current_filter, wanted_filter)


def test_analyze_invalid_filter_empty_item():
    # Current filter is invalid - since LVM will reject this filter anyway, we
    # can configure a correct filter.
    wanted_filter = ["a|^/dev/sda2$|", "r|.*|"]
    current_filter = ["a|invalid|", "r||"]
    with pytest.raises(lvmfilter.InvalidFilter):
        lvmfilter.analyze(current_filter, wanted_filter)


def test_resolve_devices_stable_names(fake_device):
    original = [
        lvmfilter.FilterItem("a", "^{}$".format(fake_device.stable_link)),
        lvmfilter.FilterItem("r", ".*"),
    ]
    resolved = [
        lvmfilter.FilterItem("a", "^{}$".format(fake_device.device)),
        lvmfilter.FilterItem("r", ".*"),
    ]
    assert lvmfilter.resolve_devices(original) == resolved


def test_resolve_devices_wild_cards():
    original = [
        lvmfilter.FilterItem("a", "^/dev/sda1$"),
        lvmfilter.FilterItem("a", "^/dev/sdb.*"),
        lvmfilter.FilterItem("r", ".*"),
    ]
    resolved = [
        lvmfilter.FilterItem("a", "^/dev/sda1$"),
        lvmfilter.FilterItem("a", "^/dev/sdb.*"),
        lvmfilter.FilterItem("r", ".*"),
    ]
    assert lvmfilter.resolve_devices(original) == resolved


def test_resolve_devices_no_anchors():
    original = [
        lvmfilter.FilterItem("a", "/dev/sda1"),
        lvmfilter.FilterItem("a", "^/dev/sdb"),
        lvmfilter.FilterItem("r", ".*"),
    ]
    resolved = [
        lvmfilter.FilterItem("a", "/dev/sda1"),
        lvmfilter.FilterItem("a", "^/dev/sdb"),
        lvmfilter.FilterItem("r", ".*"),
    ]
    assert lvmfilter.resolve_devices(original) == resolved


def test_analyze_configure_replace_unstable_device(fake_device):
    # Current filter is correct, but uses unstable device name.
    wanted_filter = ["a|^{}$|".format(fake_device.stable_link), "r|.*|"]
    current_filter = ["a|^{}$|".format(fake_device.device), "r|.*|"]
    advice = lvmfilter.analyze(current_filter, wanted_filter)
    assert advice.action == lvmfilter.CONFIGURE
    assert advice.filter == wanted_filter


def test_analyze_configure_replace_unstable_link(fake_dm_device):
    # Current filter is correct, but uses unstable link name to the device.
    wanted_filter = ["a|^{}$|".format(fake_dm_device.stable_link), "r|.*|"]
    current_filter = ["a|^{}$|".format(fake_dm_device.unstable_link), "r|.*|"]
    advice = lvmfilter.analyze(current_filter, wanted_filter)
    assert advice.action == lvmfilter.CONFIGURE
    assert advice.filter == wanted_filter


def test_analyze_recommend_replace_unstable_link_duplicate(fake_dm_device):
    # Current filter uses unstable links name to the device and there's also
    # another link with stable name to the same device.
    wanted_filter = ["a|^{}$|".format(fake_dm_device.stable_link), "r|.*|"]
    current_filter = [
        "a|^{}$|".format(fake_dm_device.unstable_link),
        "a|^{}$|".format(fake_dm_device.stable_link),
        "r|.*|",
    ]
    advice = lvmfilter.analyze(current_filter, wanted_filter)
    assert advice.action == lvmfilter.RECOMMEND
    assert advice.filter == wanted_filter


def test_analyze_recommend_replace_unstable_device_no_anchors(fake_device):
    # Current filter is correct, but uses unstable device name and don't use
    # anchors.
    wanted_filter = ["a|^{}$|".format(fake_device.stable_link), "r|.*|"]
    current_filter = ["a|{}|".format(fake_device.device), "r|.*|"]
    advice = lvmfilter.analyze(current_filter, wanted_filter)
    assert advice.action == lvmfilter.RECOMMEND
    assert advice.filter == wanted_filter


def test_analyze_recommend_links_do_not_match(tmpdir, fake_device):
    # Stable name is a link to different device than one in the filter.
    other_device = str(tmpdir.join("dm-2"))
    open(other_device, "w").close()

    wanted_filter = ["a|^{}$|".format(fake_device.stable_link), "r|.*|"]
    current_filter = ["a|^{}$|".format(other_device), "r|.*|"]
    advice = lvmfilter.analyze(current_filter, wanted_filter)
    assert advice.action == lvmfilter.RECOMMEND
    assert advice.filter == wanted_filter


def test_analyze_recommend_reg_exp_in_path(fake_device):
    # Current filter use unstable names and contains regular expression.
    wanted_filter = ["a|^{}$|".format(fake_device.stable_link), "r|.*|"]
    current_filter = ["a|^/dev/sda*$|", "r|.*|"]
    advice = lvmfilter.analyze(current_filter, wanted_filter)
    assert advice.action == lvmfilter.RECOMMEND
    assert advice.filter == wanted_filter


def test_analyze_recommend_added_custom_unstable_name(fake_device):
    # Current filter use unstable names and admin added another device with
    # unstable name.
    wanted_filter = ["a|^{}$|".format(fake_device.stable_link), "r|.*|"]
    current_filter = ["a|^/dev/sda1$|", "a|^/dev/sda2$|", "r|.*|"]
    advice = lvmfilter.analyze(current_filter, wanted_filter)
    assert advice.action == lvmfilter.RECOMMEND
    assert advice.filter == wanted_filter


def test_analyze_recommend_added_custom_stable_name(fake_device):
    # Current filter use unstable names and admin added another device with
    # stable name.
    wanted_filter = ["a|^{}$|".format(fake_device.stable_link), "r|.*|"]
    current_filter = [
        "a|^{}$|".format(fake_device.device),
        "a|^/dev/disk/by-id/lvm-pv-uuid-2d84b62d$|",
        "r|.*|",
    ]
    advice = lvmfilter.analyze(current_filter, wanted_filter)
    assert advice.action == lvmfilter.RECOMMEND
    assert advice.filter == wanted_filter


@pytest.mark.parametrize("device,expected", [
    ("fake-devices-standard", "253"),
    ("fake-devices-non-standard", "252"),
])
def test_dm_major_number(monkeypatch, device, expected):
    monkeypatch.setattr(
        lvmfilter, 'PROC_DEVICES', os.path.join(TEST_DIR, device))
    assert lvmfilter.dm_major_number() == expected


def test_dm_major_number_wrong_file_content(monkeypatch):
    monkeypatch.setattr(
        lvmfilter,
        'PROC_DEVICES',
        os.path.join(TEST_DIR, "fake-lsblk.fedora.out"))
    with pytest.raises(lvmfilter.NoDeviceMapperMajorNumber):
        lvmfilter.dm_major_number()
