#
# Copyright 2018 Red Hat, Inc.
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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#
"""
To run this test you must run the tests as root, or have writable /run/vdsm and
running supervdsm serving the user running the tests.

To setup the environment for unprivileged user:

    $ sudo mkdir /run/vdsm

    $ sudo chown $USER:$USER /run/vdsm

    $ sudo env PYTHONPATH=lib static/usr/sbin/supervdsmd \
          --data-center /var/tmp/vdsm/data-center \
          --sockfile /run/vdsm/svdsm.sock \
          --user=$USER \
          --group=$USER \
          --logger-conf tests/conf/svdsm.logger.conf \
          --disable-gluster \
          --disable-network
"""

from __future__ import absolute_import
from __future__ import division

import io
import os
import uuid

from contextlib import contextmanager

import pytest

from vdsm.common import cmdutils
from vdsm.common import supervdsm
from vdsm.storage import constants as sc
from vdsm.storage import exception as se
from vdsm.storage import nbd
from vdsm.storage import qemuimg

from . marks import xfail_python3
from . storagetestlib import fake_env

# TODO: Move to actual code when we support preallocated qcow2 images.
PREALLOCATION = {
    sc.RAW_FORMAT: qemuimg.PREALLOCATION.FALLOC,
    sc.COW_FORMAT: qemuimg.PREALLOCATION.METADATA,
}


def have_supervdsm():
    return os.access(supervdsm.ADDRESS, os.W_OK)


def is_root():
    return os.geteuid() == 0


requires_privileges = pytest.mark.skipif(
    not (is_root() or have_supervdsm()),
    reason="requires root or running supervdsm")


broken_on_ci = pytest.mark.skipif(
    "OVIRT_CI" in os.environ or "TRAVIS_CI" in os.environ,
    reason="requires systemd daemon able to run services")


@pytest.fixture
def nbd_env(monkeypatch):
    """
    Fixture for serving a volume using nbd server.
    """
    # These tests require supervdsm running, so we cannot use a random
    # directory. We need to use the same path used to start supervdsm.
    data_center = "/var/tmp/vdsm/data-center"

    with fake_env("file", data_center=data_center) as env:
        env.virtual_size = 1024**2

        # Source image for copying into the nbd server.
        env.src = os.path.join(env.tmpdir, "src")
        with io.open(env.src, "wb") as f:
            f.truncate(env.virtual_size)
            f.seek(128 * 1024)
            f.write("data from source image")

        # Destination for copying from nbd server.
        env.dst = os.path.join(env.tmpdir, "dst")

        yield env


@broken_on_ci
@requires_privileges
@pytest.mark.parametrize("format", [sc.COW_FORMAT, sc.RAW_FORMAT])
@pytest.mark.parametrize("allocation", [sc.SPARSE_VOL, sc.PREALLOCATED_VOL])
@pytest.mark.parametrize("discard", [True, False])
def test_roundtrip(nbd_env, format, allocation, discard):
    # Volume served by qemu-nd.
    img_id = str(uuid.uuid4())
    vol_id = str(uuid.uuid4())
    nbd_env.make_volume(
        nbd_env.virtual_size,
        img_id,
        vol_id,
        vol_format=format,
        prealloc=allocation)

    # Server configuration.
    config = {
        "sd_id": nbd_env.sd_manifest.sdUUID,
        "img_id": img_id,
        "vol_id": vol_id,
        "discard": discard,
    }

    with nbd_server(config) as nbd_url:
        # Copy data from source to NBD server, and from NBD server to dst.
        # Both files should match byte for byte after the operation.
        op = qemuimg.convert(
            nbd_env.src, nbd_url, srcFormat="raw", create=False)
        op.run()
        op = qemuimg.convert(nbd_url, nbd_env.dst, dstFormat="raw")
        op.run()

    with io.open(nbd_env.src) as s, io.open(nbd_env.dst) as d:
        assert s.read() == d.read()

    # Now the server should not be accessible.
    with pytest.raises(cmdutils.Error):
        qemuimg.info(nbd_url)


@broken_on_ci
@requires_privileges
@pytest.mark.parametrize("format", [sc.COW_FORMAT, sc.RAW_FORMAT])
@pytest.mark.parametrize("allocation", [sc.SPARSE_VOL, sc.PREALLOCATED_VOL])
def test_readonly(nbd_env, format, allocation):
    # Volume served by qemu-nd.
    img_id = str(uuid.uuid4())
    vol_id = str(uuid.uuid4())
    nbd_env.make_volume(
        nbd_env.virtual_size,
        img_id,
        vol_id,
        vol_format=format,
        prealloc=allocation)

    # Fill volume with data before starting the server.
    vol = nbd_env.sd_manifest.produceVolume(img_id, vol_id)
    op = qemuimg.convert(
        nbd_env.src,
        vol.getVolumePath(),
        dstFormat=sc.fmt2str(format),
        preallocation=PREALLOCATION.get(format))
    op.run()

    # Server configuration.
    config = {
        "sd_id": nbd_env.sd_manifest.sdUUID,
        "img_id": img_id,
        "vol_id": vol_id,
        "readonly": True,
    }

    with nbd_server(config) as nbd_url:
        # Writing to NBD server must fail.
        with pytest.raises(cmdutils.Error):
            op = qemuimg.convert(
                nbd_env.src, nbd_url, srcFormat="raw", create=False)
            op.run()

        # Copy data from NBD server to dst. Both files should match byte
        # for byte after the operation.
        op = qemuimg.convert(nbd_url, nbd_env.dst, dstFormat="raw")
        op.run()

    with io.open(nbd_env.src) as s, io.open(nbd_env.dst) as d:
        assert s.read() == d.read()

    # Now the server should not be accessible.
    with pytest.raises(cmdutils.Error):
        qemuimg.info(nbd_url)


@xfail_python3
def test_shared_volume():
    with fake_env("file") as env:
        img_id = str(uuid.uuid4())
        vol_id = str(uuid.uuid4())
        env.make_volume(1024**3, img_id, vol_id)
        vol = env.sd_manifest.produceVolume(img_id, vol_id)
        vol.setShared()

        config = {
            "sd_id": env.sd_manifest.sdUUID,
            "img_id": img_id,
            "vol_id": vol_id,
        }

        with pytest.raises(se.SharedVolumeNonWritable):
            nbd.start_server("no-server", config)


@broken_on_ci
def test_stop_server_not_running():
    # Stopping non-existing server should succeed.
    nbd.stop_server("no-such-server-uuid")


@contextmanager
def nbd_server(config):
    server_id = str(uuid.uuid4())
    nbd_url = nbd.start_server(server_id, config)
    try:
        yield nbd_url
    finally:
        nbd.stop_server(server_id)
