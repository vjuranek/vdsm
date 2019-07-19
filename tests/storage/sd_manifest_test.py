# Copyright 2015-2017 Red Hat, Inc.
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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#

from __future__ import absolute_import
from __future__ import division

import os
import uuid

import pytest

from vdsm.storage import constants as sc
from vdsm.storage import exception as se
from vdsm.storage import clusterlock
from vdsm.storage import sd

from testlib import recorded
from testlib import make_uuid

from storage.storagetestlib import (
    fake_block_env,
    fake_file_env,
    make_file_volume,
)

from . marks import xfail_python3

MB = 1048576

# We want to create volumes larger than the minimum block volume size
# (currently 128M)
VOLSIZE = 256 * MB


@xfail_python3
class ManifestMixin(object):

    def test_init_failure_raises(self, monkeypatch):
        def fail(sdUUID, idsPath, lease, alignment=sc.ALIGNMENT_1M,
                 block_size=sc.BLOCK_SIZE_512):
            raise RuntimeError("injected failure")

        with self.env() as env:
            monkeypatch.setattr(clusterlock, 'initSANLock', fail)
            with pytest.raises(RuntimeError):
                env.sd_manifest.initDomainLock()


@xfail_python3
class TestFileManifest(ManifestMixin):
    env = fake_file_env

    def setup_method(self):
        self.img_id = str(uuid.uuid4())
        self.vol_id = str(uuid.uuid4())

    def test_get_monitoring_path(self):
        with self.env() as env:
            assert (env.sd_manifest.metafile ==
                    env.sd_manifest.getMonitoringPath())

    def test_getvsize(self):
        with self.env() as env:
            make_file_volume(env.sd_manifest, VOLSIZE,
                             self.img_id, self.vol_id)
            assert VOLSIZE == env.sd_manifest.getVSize(
                self.img_id, self.vol_id)

    def test_getvallocsize(self):
        with self.env() as env:
            make_file_volume(env.sd_manifest, VOLSIZE,
                             self.img_id, self.vol_id)
            assert 0 == env.sd_manifest.getVAllocSize(
                self.img_id, self.vol_id)

    def test_getisodomainimagesdir(self):
        with self.env() as env:
            isopath = os.path.join(env.sd_manifest.domaindir, sd.DOMAIN_IMAGES,
                                   sd.ISO_IMAGE_UUID)
            assert isopath == env.sd_manifest.getIsoDomainImagesDir()

    def test_getmdpath(self):
        with self.env() as env:
            sd_manifest = env.sd_manifest
            mdpath = os.path.join(sd_manifest.domaindir, sd.DOMAIN_META_DATA)
            assert mdpath == env.sd_manifest.getMDPath()

    def test_getmetaparam(self):
        with self.env() as env:
            sd_manifest = env.sd_manifest
            assert (sd_manifest.sdUUID ==
                    sd_manifest.getMetaParam(sd.DMDK_SDUUID))

    def test_getallimages(self):
        with self.env() as env:
            assert set() == env.sd_manifest.getAllImages()
            img_id = str(uuid.uuid4())
            vol_id = str(uuid.uuid4())
            make_file_volume(env.sd_manifest, VOLSIZE, img_id, vol_id)
            assert img_id in env.sd_manifest.getAllImages()

    def test_purgeimage_race(self):
        with self.env() as env:
            sd_id = env.sd_manifest.sdUUID
            img_id = str(uuid.uuid4())
            vol_id = str(uuid.uuid4())
            make_file_volume(env.sd_manifest, VOLSIZE, img_id, vol_id)

            env.sd_manifest.deleteImage(sd_id, img_id, None)
            # Simulate StorageDomain.imageGarbageCollector by removing the
            # deleted image directory.
            deleted_dir = env.sd_manifest.getDeletedImagePath(img_id)
            env.sd_manifest.oop.fileUtils.cleanupdir(deleted_dir)
            # purgeImage should not raise if the image was already removed
            env.sd_manifest.purgeImage(sd_id, img_id, [vol_id], False)


@xfail_python3
class TestBlockManifest(ManifestMixin):
    env = fake_block_env

    def test_get_monitoring_path(self):
        with self.env() as env:
            md_lv_path = env.lvm.lvPath(env.sd_manifest.sdUUID, sd.METADATA)
            assert md_lv_path == env.sd_manifest.getMonitoringPath()

    def test_getvsize_active_lv(self):
        # Tests the path when the device file is present
        with self.env() as env:
            vg_name = env.sd_manifest.sdUUID
            lv_name = str(uuid.uuid4())
            env.lvm.createLV(vg_name, lv_name, VOLSIZE // MB)
            env.lvm.fake_lv_symlink_create(vg_name, lv_name)
            assert VOLSIZE == env.sd_manifest.getVSize('<imgUUID>', lv_name)

    def test_getvsize_inactive_lv(self):
        # Tests the path when the device file is not present
        with self.env() as env:
            lv_name = str(uuid.uuid4())
            env.lvm.createLV(env.sd_manifest.sdUUID, lv_name, VOLSIZE // MB)
            assert VOLSIZE == env.sd_manifest.getVSize('<imgUUID>', lv_name)

    def test_getmetaparam(self):
        with self.env() as env:
            assert (env.sd_manifest.sdUUID ==
                    env.sd_manifest.getMetaParam(sd.DMDK_SDUUID))


@xfail_python3
class TestBlockDomainMetadataSlot:

    # Note: the first 4 slots (0-3) are reserved for domain metadata in V3,4
    @pytest.mark.parametrize("used_slots, free_slot", [
        [[], 4],
        [[4], 5],
        [[5], 4],
        [[4, 6], 5],
        [[4, 7], 5],
    ])
    @pytest.mark.parametrize("sd_version", [3, 4])
    def test_metaslot_selection_v4(self, used_slots, free_slot, sd_version):
        self._metaslot_selection(used_slots, free_slot, sd_version)

    @pytest.mark.parametrize("used_slots, free_slot", [
        [[], 1],
        [[1], 2],
        [[2], 1],
        [[1, 3], 2],
        [[1, 4], 2],
    ])
    def test_metaslot_selection_v5(self, used_slots, free_slot):
        self._metaslot_selection(used_slots, free_slot, 5)

    def _metaslot_selection(self, used_slots, free_slot, sd_version):
        with fake_block_env(sd_version=sd_version) as env:
            for offset in used_slots:
                lv = make_uuid()
                sduuid = env.sd_manifest.sdUUID
                env.lvm.createLV(sduuid, lv, VOLSIZE // MB)
                tag = sc.TAG_PREFIX_MD + str(offset)
                env.lvm.addtag(sduuid, lv, tag)
            with env.sd_manifest.acquireVolumeMetadataSlot(None) as mdSlot:
                assert mdSlot == free_slot

    @pytest.mark.parametrize("sd_version", [3, 4, 5])
    def test_metaslot_lock(self, sd_version):
        with fake_block_env(sd_version=sd_version) as env:
            with env.sd_manifest.acquireVolumeMetadataSlot(None):
                acquired = env.sd_manifest._lvTagMetaSlotLock.acquire(False)
                assert not acquired


class StorageDomainManifest(sd.StorageDomainManifest):
    def __init__(self):
        pass

    @recorded
    def acquireDomainLock(self, host_id):
        pass

    @recorded
    def releaseDomainLock(self):
        pass

    @recorded
    def dummy(self):
        pass


class TestDomainLock():

    def test_domainlock_contextmanager(self):
        expected_calls = [("acquireDomainLock", (1,), {}),
                          ("dummy", (), {}),
                          ("releaseDomainLock", (), {})]
        manifest = StorageDomainManifest()
        with manifest.domain_lock(1):
            manifest.dummy()
        assert manifest.__calls__ == expected_calls

    def test_domainlock_contextmanager_exception(self):
        class InjectedFailure(Exception):
            pass

        expected_calls = [("acquireDomainLock", (1,), {}),
                          ("releaseDomainLock", (), {})]
        manifest = StorageDomainManifest()
        with pytest.raises(InjectedFailure):
            with manifest.domain_lock(1):
                raise InjectedFailure()
        assert manifest.__calls__ == expected_calls


class TestCreateVolumeParams:

    @pytest.mark.parametrize("vol_format", sc.VOL_FORMAT)
    def test_valid_format(self, vol_format):
        StorageDomainManifest.validateCreateVolumeParams(
            vol_format, sc.BLANK_UUID)

    def test_invalid_format(self):
        with pytest.raises(se.IncorrectFormat):
            StorageDomainManifest.validateCreateVolumeParams(
                -1, sc.BLANK_UUID)

    @pytest.mark.parametrize("disk_type", sc.VOL_DISKTYPE)
    def test_valid_type(self, disk_type):
        StorageDomainManifest.validateCreateVolumeParams(
            sc.RAW_FORMAT, sc.BLANK_UUID, diskType=disk_type)

    def test_invalid_type(self):
        with pytest.raises(se.InvalidParameterException):
            StorageDomainManifest.validateCreateVolumeParams(
                sc.RAW_FORMAT, sc.BLANK_UUID, diskType="FAIL")

    def test_invalid_parent(self):
        with pytest.raises(se.IncorrectFormat):
            StorageDomainManifest.validateCreateVolumeParams(
                sc.RAW_FORMAT, "11111111-1111-1111-1111-11111111111")

    @pytest.mark.parametrize("preallocate", sc.VOL_TYPE)
    def test_valid_preallocate(self, preallocate):
        StorageDomainManifest.validateCreateVolumeParams(
            sc.RAW_FORMAT, sc.BLANK_UUID, preallocate=preallocate)

    def test_invalid_preallocate(self):
        with pytest.raises(se.IncorrectType):
            StorageDomainManifest.validateCreateVolumeParams(
                sc.RAW_FORMAT, sc.BLANK_UUID, preallocate=-1)
