#
# Copyright 2020 Red Hat, Inc.
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

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import libvirt
import os
import pytest

from fakelib import FakeLogger
from testlib import make_uuid
from testlib import maybefail

from vdsm.common import exception
from vdsm.common import nbdutils
from vdsm.common.xmlutils import indented

from vdsm.storage import hsm
from vdsm.storage import transientdisk
from vdsm.storage.dispatcher import Dispatcher

from vdsm.virt import backup

from virt.fakedomainadapter import FakeCheckpoint
from virt.fakedomainadapter import FakeDomainAdapter

from . import vmfakelib as fake

requires_backup_support = pytest.mark.skipif(
    not backup.backup_enabled,
    reason="libvirt does not support backup")

DOMAIN_ID = make_uuid()
VOLUME_ID = make_uuid()
# Full backup parameters
BACKUP_1_ID = make_uuid()
CHECKPOINT_1_ID = make_uuid()
CHECKPOINT_1_XML = """
    <domaincheckpoint>
      <name>{}</name>
      <description>checkpoint for backup '{}'</description>
      <disks>
        <disk name='sda' checkpoint='bitmap'/>
        <disk name='vda' checkpoint='bitmap'/>
      </disks>
    </domaincheckpoint>
    """.format(CHECKPOINT_1_ID, BACKUP_1_ID)

# Incremental backup parameters
BACKUP_2_ID = make_uuid()
CHECKPOINT_2_ID = make_uuid()
CHECKPOINT_2_XML = """
    <domaincheckpoint>
      <name>{}</name>
      <description>checkpoint for backup '{}'</description>
      <parent>
        <name>{}</name>
      </parent>
      <disks>
        <disk name='sda' checkpoint='bitmap'/>
        <disk name='vda' checkpoint='bitmap'/>
      </disks>
    </domaincheckpoint>
    """.format(CHECKPOINT_2_ID, BACKUP_2_ID, CHECKPOINT_1_ID)

MIXED_CHECKPOINT_XML = """
    <domaincheckpoint>
      <name>{}</name>
      <description>checkpoint for backup '{}'</description>
      <disks>
        <disk name='sda' checkpoint='bitmap'/>
      </disks>
    </domaincheckpoint>
    """.format(CHECKPOINT_1_ID, BACKUP_1_ID)

CHECKPOINT_1 = FakeCheckpoint(CHECKPOINT_1_XML, CHECKPOINT_1_ID)
CHECKPOINT_2 = FakeCheckpoint(CHECKPOINT_2_XML, CHECKPOINT_2_ID)


class FakeDrive(object):

    def __init__(self, name, imageID, path=''):
        self.name = name
        self.imageID = imageID
        self.diskType = 'file'
        self.path = path
        self.format = 'cow'
        self.domainID = 'domain_id'


class FakeHSM(hsm.HSM):

    def __init__(self):
        self._ready = True

    @property
    def ready(self):
        return self._ready


class FakeClientIF(object):

    def __init__(self):
        self.irs = Dispatcher(FakeHSM())


class FakeVm(object):

    def __init__(self):
        self.id = "vm_id"
        self.log = FakeLogger()
        self.cif = FakeClientIF()
        self.froze = False
        self.thawed = False
        self.errors = {}

    def findDriveByUUIDs(self, disk):
        return FAKE_DRIVES[disk['imageID']]

    def find_device_by_name_or_path(self, disk_name):
        for fake_drive in FAKE_DRIVES.values():
            if fake_drive.name == disk_name:
                return fake_drive

        raise LookupError("Disk %s not found" % disk_name)

    @maybefail
    def freeze(self):
        self.froze = True

    def thaw(self):
        self.thawed = True


IMAGE_1_UUID = make_uuid()
IMAGE_2_UUID = make_uuid()

FAKE_DRIVES = {
    IMAGE_1_UUID:
        FakeDrive(name="sda", imageID=IMAGE_1_UUID, path="/path/to/backing1"),
    IMAGE_2_UUID:
        FakeDrive(name="vda", imageID=IMAGE_2_UUID, path="/path/to/backing2"),
}

FAKE_SCRATCH_DISKS = {
    "sda": "/path/to/scratch_sda",
    "vda": "/path/to/scratch_vda",
}


FAKE_CHECKPOINT_CFG = [
    {
        'id': CHECKPOINT_1_ID,
        'xml': CHECKPOINT_1_XML
    },
    {
        'id': CHECKPOINT_2_ID,
        'xml': CHECKPOINT_2_XML
    },
]


@pytest.fixture
def tmp_backupdir(tmpdir, monkeypatch):
    path = str(tmpdir.join("backup"))
    monkeypatch.setattr(backup, 'P_BACKUP', path)


@pytest.fixture
def tmp_basedir(tmpdir, monkeypatch):
    path = str(tmpdir.join("transient_disks"))
    monkeypatch.setattr(transientdisk, 'P_TRANSIENT_DISKS', path)


@requires_backup_support
def test_start_stop_backup(tmp_backupdir, tmp_basedir):
    vm = FakeVm()

    socket_path = backup.socket_path(BACKUP_1_ID)
    scratch_disk_paths = _get_scratch_disks_path(BACKUP_1_ID)

    expected_xml = """
        <domainbackup mode='pull'>
            <server transport='unix' socket='{}'/>
            <disks>
                <disk name='sda' type='file'>
                    <scratch file='{}'>
                        <seclabel model="dac" relabel="no"/>
                    </scratch>
                </disk>
                <disk name='vda' type='file'>
                    <scratch file='{}'>
                        <seclabel model="dac" relabel="no"/>
                    </scratch>
                </disk>
            </disks>
        </domainbackup>
        """.format(socket_path, scratch_disk_paths[0], scratch_disk_paths[1])

    dom = FakeDomainAdapter()
    fake_disks = create_fake_disks()
    config = {
        'backup_id': BACKUP_1_ID,
        'disks': fake_disks
    }

    res = backup.start_backup(vm, dom, config)
    assert indented(expected_xml) == indented(dom.input_backup_xml)
    assert dom.backing_up

    verify_scratch_disks_exists(vm)

    # verify that the vm froze and thawed during the backup
    assert vm.froze
    assert vm.thawed

    assert 'checkpoint' not in res['result']
    result_disks = res['result']['disks']
    verify_backup_urls(BACKUP_1_ID, result_disks)

    backup.stop_backup(vm, dom, BACKUP_1_ID)
    assert not dom.backing_up

    verify_scratch_disks_removed(vm)


@requires_backup_support
@pytest.mark.parametrize(
    "disks_in_checkpoint, expected_checkpoint_xml", [
        ([IMAGE_1_UUID, IMAGE_2_UUID], CHECKPOINT_1_XML),
        ([IMAGE_1_UUID], MIXED_CHECKPOINT_XML),
    ], ids=["cow", "mix"]
)
def test_start_stop_backup_with_checkpoint(
        tmp_backupdir, tmp_basedir,
        disks_in_checkpoint, expected_checkpoint_xml):
    vm = FakeVm()
    dom = FakeDomainAdapter()

    fake_disks = create_fake_disks(disks_in_checkpoint)
    config = {
        'backup_id': BACKUP_1_ID,
        'disks': fake_disks,
        'to_checkpoint_id': CHECKPOINT_1_ID
    }

    res = backup.start_backup(vm, dom, config)
    assert dom.backing_up
    assert indented(expected_checkpoint_xml) == (
        indented(dom.input_checkpoint_xml))

    verify_scratch_disks_exists(vm)

    # verify that the vm froze and thawed during the backup
    assert vm.froze
    assert vm.thawed

    result_disks = res['result']['disks']
    verify_backup_urls(BACKUP_1_ID, result_disks)

    backup.stop_backup(vm, dom, BACKUP_1_ID)
    assert not dom.backing_up

    verify_scratch_disks_removed(vm)


@requires_backup_support
def test_incremental_backup(tmp_backupdir, tmp_basedir):
    vm = FakeVm()
    dom = FakeDomainAdapter()
    fake_disks = create_fake_disks()

    # start full backup
    config = {
        'backup_id': BACKUP_1_ID,
        'disks': fake_disks,
        'to_checkpoint_id': CHECKPOINT_1_ID
    }

    res = backup.start_backup(vm, dom, config)
    assert dom.backing_up

    result_disks = res['result']['disks']
    verify_backup_urls(BACKUP_1_ID, result_disks)

    backup.stop_backup(vm, dom, BACKUP_1_ID)
    assert not dom.backing_up

    verify_scratch_disks_removed(vm)

    # start incremental backup
    socket_path = backup.socket_path(BACKUP_2_ID)
    scratch_disk_paths = _get_scratch_disks_path(BACKUP_2_ID)

    expected_xml = """
        <domainbackup mode='pull'>
            <incremental>{}</incremental>
            <server transport='unix' socket='{}'/>
            <disks>
                <disk name='sda' type='file'>
                    <scratch file='{}'>
                        <seclabel model="dac" relabel="no"/>
                    </scratch>
                </disk>
                <disk name='vda' type='file'>
                    <scratch file='{}'>
                        <seclabel model="dac" relabel="no"/>
                    </scratch>
                </disk>
            </disks>
        </domainbackup>
        """.format(
        CHECKPOINT_1_ID,
        socket_path,
        scratch_disk_paths[0],
        scratch_disk_paths[1])

    dom.output_checkpoints = [CHECKPOINT_1]

    config = {
        'backup_id': BACKUP_2_ID,
        'disks': fake_disks,
        'from_checkpoint_id': CHECKPOINT_1_ID,
        'to_checkpoint_id': CHECKPOINT_2_ID,
        'parent_checkpoint_id': CHECKPOINT_1_ID
    }

    res = backup.start_backup(vm, dom, config)
    assert dom.backing_up
    assert indented(expected_xml) == indented(dom.input_backup_xml)
    assert indented(CHECKPOINT_2_XML) == (
        indented(dom.input_checkpoint_xml))

    verify_scratch_disks_exists(vm, BACKUP_2_ID)

    # verify that the vm froze and thawed during the backup
    assert vm.froze
    assert vm.thawed

    result_disks = res['result']['disks']
    verify_backup_urls(BACKUP_2_ID, result_disks)

    backup.stop_backup(vm, dom, BACKUP_2_ID)
    verify_scratch_disks_removed(vm)


@requires_backup_support
def test_start_backup_failed_get_checkpoint(tmp_backupdir, tmp_basedir):
    vm = FakeVm()
    dom = FakeDomainAdapter()
    dom.errors["checkpointLookupByName"] = fake.libvirt_error(
        [libvirt.VIR_ERR_INTERNAL_ERROR], "Fake libvirt error")

    fake_disks = create_fake_disks()
    config = {
        'backup_id': BACKUP_1_ID,
        'disks': fake_disks,
        'to_checkpoint_id': CHECKPOINT_1_ID
    }

    res = backup.start_backup(vm, dom, config)
    assert dom.backing_up

    verify_scratch_disks_exists(vm)

    # verify that the vm froze and thawed during the backup
    assert vm.froze
    assert vm.thawed

    assert 'checkpoint' not in res['result']
    result_disks = res['result']['disks']
    verify_backup_urls(BACKUP_1_ID, result_disks)

    backup.stop_backup(vm, dom, BACKUP_1_ID)
    assert not dom.backing_up

    verify_scratch_disks_removed(vm)


@requires_backup_support
def test_start_backup_disk_not_found():
    vm = FakeVm()
    dom = FakeDomainAdapter()

    fake_disks = create_fake_disks()
    fake_disks.append({
        'domainID': make_uuid(),
        'imageID': make_uuid(),
        'volumeID': make_uuid(),
        'checkpoint': False
    })

    config = {
        'backup_id': BACKUP_1_ID,
        'disks': fake_disks
    }

    with pytest.raises(exception.BackupError):
        backup.start_backup(vm, dom, config)

    assert not dom.backing_up
    verify_scratch_disks_removed(vm)

    # verify that the vm didn't froze or thawed during the backup
    assert not vm.froze
    assert not vm.thawed


@requires_backup_support
def test_backup_begin_failed(tmp_backupdir, tmp_basedir):
    vm = FakeVm()
    dom = FakeDomainAdapter()
    dom.errors["backupBegin"] = fake.libvirt_error(
        [libvirt.VIR_ERR_INTERNAL_ERROR], "Fake libvirt error")

    fake_disks = create_fake_disks()

    config = {
        'backup_id': BACKUP_1_ID,
        'disks': fake_disks
    }

    with pytest.raises(exception.BackupError):
        backup.start_backup(vm, dom, config)

    verify_scratch_disks_removed(vm)

    # verify that the vm froze and thawed during the backup
    assert vm.froze
    assert vm.thawed


@requires_backup_support
def test_backup_begin_freeze_failed(tmp_backupdir, tmp_basedir):
    vm = FakeVm()
    vm.errors["freeze"] = fake.libvirt_error(
        [libvirt.VIR_ERR_INTERNAL_ERROR], "Fake libvirt error")
    dom = FakeDomainAdapter()

    fake_disks = create_fake_disks()

    config = {
        'backup_id': BACKUP_1_ID,
        'disks': fake_disks
    }

    with pytest.raises(libvirt.libvirtError):
        backup.start_backup(vm, dom, config)

    verify_scratch_disks_removed(vm)

    # verify that the vm didn't froze but thawed during the backup
    assert not vm.froze
    assert vm.thawed


def test_backup_begin_failed_no_disks(tmp_backupdir, tmp_basedir):
    vm = FakeVm()
    dom = FakeDomainAdapter()

    config = {
        'backup_id': BACKUP_1_ID,
        'disks': ()
    }

    with pytest.raises(exception.BackupError):
        backup.start_backup(vm, dom, config)


def test_backup_begin_failed_no_parent(tmp_backupdir, tmp_basedir):
    vm = FakeVm()
    dom = FakeDomainAdapter()
    fake_disks = create_fake_disks()

    config = {
        'backup_id': BACKUP_1_ID,
        'disks': fake_disks,
        'from_checkpoint_id': CHECKPOINT_1_ID
    }

    with pytest.raises(exception.BackupError):
        backup.start_backup(vm, dom, config)


@requires_backup_support
def test_stop_backup_failed(tmp_backupdir, tmp_basedir):
    vm = FakeVm()
    dom = FakeDomainAdapter()
    dom.errors["abortJob"] = fake.libvirt_error(
        [libvirt.VIR_ERR_INTERNAL_ERROR], "Fake libvirt error")

    fake_disks = create_fake_disks()

    config = {
        'backup_id': BACKUP_1_ID,
        'disks': fake_disks
    }

    res = backup.start_backup(vm, dom, config)

    verify_scratch_disks_exists(vm)

    result_disks = res['result']['disks']
    verify_backup_urls(BACKUP_1_ID, result_disks)

    with pytest.raises(exception.BackupError):
        backup.stop_backup(vm, dom, BACKUP_1_ID)

    # Failed to stop, backup still alive
    assert dom.backing_up

    # verify scratch disks weren't removed
    verify_scratch_disks_exists(vm)


@requires_backup_support
def test_stop_non_existing_backup():
    vm = FakeVm()
    dom = FakeDomainAdapter()
    dom.errors["backupGetXMLDesc"] = fake.libvirt_error(
        [libvirt.VIR_ERR_NO_DOMAIN_BACKUP], "Fake libvirt error")

    # test that nothing is raised when stopping non-existing backup
    backup.stop_backup(vm, dom, BACKUP_1_ID)


@requires_backup_support
def test_backup_info(tmp_backupdir, tmp_basedir):
    vm = FakeVm()
    expected_xml = """
        <domainbackup mode='pull'>
          <server transport='unix' socket='{}'/>
          <disks>
            <disk name='sda' backup='yes' type='file' exportname='sda'>
                <driver type='qcow2'/>
                <scratch file='/path/to/scratch_sda'>
                    <seclabel model='dac' relabel='no'/>
                </scratch>
            </disk>
            <disk name='vda' backup='yes' type='file' exportname='vda'>
                <driver type='qcow2'/>
                <scratch file='/path/to/scratch_vda'>
                    <seclabel model="dac" relabel="no"/>
                </scratch>
            </disk>
            <disk name='hdc' backup='no'/>
          </disks>
        </domainbackup>
        """.format(backup.socket_path(BACKUP_1_ID))
    dom = FakeDomainAdapter(output_backup_xml=expected_xml)

    fake_disks = create_fake_disks()
    config = {
        'backup_id': BACKUP_1_ID,
        'disks': fake_disks
    }
    res = backup.start_backup(vm, dom, config)
    backup_info = backup.backup_info(vm, dom, BACKUP_1_ID)

    assert res['result']['disks'] == backup_info['result']['disks']
    assert 'checkpoint' not in backup_info['result']


@requires_backup_support
def test_backup_info_no_backup_running():
    vm = FakeVm()
    dom = FakeDomainAdapter()
    dom.errors["backupGetXMLDesc"] = fake.libvirt_error(
        [libvirt.VIR_ERR_NO_DOMAIN_BACKUP], "Fake libvirt error")

    with pytest.raises(exception.NoSuchBackupError):
        backup.backup_info(vm, dom, BACKUP_1_ID)


@requires_backup_support
def test_backup_info_get_xml_desc_failed():
    vm = FakeVm()
    dom = FakeDomainAdapter()
    dom.errors["backupGetXMLDesc"] = fake.libvirt_error(
        [libvirt.VIR_ERR_INTERNAL_ERROR], "Fakse libvirt error")

    with pytest.raises(exception.BackupError):
        backup.backup_info(vm, dom, BACKUP_1_ID)


@requires_backup_support
def test_fail_parse_backup_xml(tmp_backupdir, tmp_basedir):
    vm = FakeVm()
    INVALID_BACKUP_XML = """
        <domainbackup mode='pull'>
            <disks/>
        </domainbackup>
        """
    dom = FakeDomainAdapter(output_backup_xml=INVALID_BACKUP_XML)

    fake_disks = create_fake_disks()
    config = {
        'backup_id': BACKUP_1_ID,
        'disks': fake_disks
    }
    backup.start_backup(vm, dom, config)

    with pytest.raises(exception.BackupError):
        backup.backup_info(vm, dom, BACKUP_1_ID)


@requires_backup_support
def test_list_checkpoints():
    dom = FakeDomainAdapter(output_checkpoints=[CHECKPOINT_1, CHECKPOINT_2])

    vm = FakeVm()
    res = backup.list_checkpoints(vm, dom)

    assert res["result"] == [CHECKPOINT_1.getName(), CHECKPOINT_2.getName()]


@requires_backup_support
def test_list_empty_checkpoints():
    dom = FakeDomainAdapter()
    vm = FakeVm()
    res = backup.list_checkpoints(vm, dom)

    assert res["result"] == []


@requires_backup_support
def test_redefine_checkpoints_succeeded():
    dom = FakeDomainAdapter(output_checkpoints=[CHECKPOINT_1, CHECKPOINT_2])

    vm = FakeVm()
    res = backup.redefine_checkpoints(vm, dom, FAKE_CHECKPOINT_CFG)

    expected_result = {
        'checkpoint_ids': [CHECKPOINT_1.getName(), CHECKPOINT_2.getName()],
    }
    assert res["result"] == expected_result


@requires_backup_support
def test_redefine_checkpoints_failed():
    dom = FakeDomainAdapter()
    # simulating an error that raised during
    # checkpointCreateXML() method in libvirt.
    error_msg = "Create checkpoint XML Error"
    dom.errors["checkpointCreateXML"] = fake.libvirt_error(
        [libvirt.VIR_ERR_INTERNAL_ERROR, '', error_msg], "Fake libvirt error")
    vm = FakeVm()

    res = backup.redefine_checkpoints(vm, dom, FAKE_CHECKPOINT_CFG)

    expected_result = {
        'checkpoint_ids': [],
        'error': {
            'code': 1,
            'message': error_msg
        }
    }
    assert res["result"] == expected_result


@requires_backup_support
def test_redefine_checkpoints_failed_after_one_succeeded():
    dom = FakeDomainAdapter(output_checkpoints=[CHECKPOINT_1, CHECKPOINT_2])

    vm = FakeVm()
    # Add non existing checkpoint to FAKE_CHECKPOINT_CFG
    # to fail the validation in checkpointCreateXML
    cfg = list(FAKE_CHECKPOINT_CFG)
    cfg.append({'id': make_uuid(), 'xml': "<xml/>"})
    res = backup.redefine_checkpoints(vm, dom, cfg)

    expected_result = {
        'checkpoint_ids': [CHECKPOINT_1.getName(), CHECKPOINT_2.getName()],
        'error': {
            'code': 102,
            'message': "Invalid checkpoint error"
        }
    }
    assert res["result"] == expected_result


@requires_backup_support
def test_delete_all_checkpoints():
    dom = FakeDomainAdapter()
    dom.output_checkpoints = [
        FakeCheckpoint(CHECKPOINT_1_XML, CHECKPOINT_1_ID, dom=dom),
        FakeCheckpoint(CHECKPOINT_2_XML, CHECKPOINT_2_ID, dom=dom)
    ]

    vm = FakeVm()
    res = backup.delete_checkpoints(
        vm, dom, [CHECKPOINT_1_ID, CHECKPOINT_2_ID])

    expected_result = {
        'checkpoint_ids': [CHECKPOINT_1_ID, CHECKPOINT_2_ID]
    }
    assert res["result"] == expected_result

    res = backup.list_checkpoints(vm, dom)
    assert res["result"] == []


@requires_backup_support
def test_delete_one_checkpoint():
    dom = FakeDomainAdapter()
    dom.output_checkpoints = [
        FakeCheckpoint(CHECKPOINT_1_XML, CHECKPOINT_1_ID, dom=dom),
        FakeCheckpoint(CHECKPOINT_2_XML, CHECKPOINT_2_ID, dom=dom)
    ]

    vm = FakeVm()
    res = backup.delete_checkpoints(vm, dom, [CHECKPOINT_1_ID])

    expected_result = {
        'checkpoint_ids': [CHECKPOINT_1_ID]
    }
    assert res["result"] == expected_result

    res = backup.list_checkpoints(vm, dom)
    assert res["result"] == [CHECKPOINT_2_ID]


@requires_backup_support
def test_delete_missing_checkpoint():
    dom = FakeDomainAdapter()
    dom.output_checkpoints = [
        FakeCheckpoint(CHECKPOINT_2_XML, CHECKPOINT_2_ID, dom=dom)
    ]

    vm = FakeVm()
    res = backup.delete_checkpoints(
        vm, dom, [CHECKPOINT_1_ID, CHECKPOINT_2_ID])

    expected_result = {
        'checkpoint_ids': [CHECKPOINT_1_ID, CHECKPOINT_2_ID]
    }
    # validate that the missing checkpoint reported as
    # successfully removed
    assert res["result"] == expected_result

    res = backup.list_checkpoints(vm, dom)
    assert res["result"] == []


@requires_backup_support
def test_delete_checkpoint_from_empty_chain():
    dom = FakeDomainAdapter()
    vm = FakeVm()

    res = backup.delete_checkpoints(vm, dom, [CHECKPOINT_1_ID])

    expected_result = {
        'checkpoint_ids': [CHECKPOINT_1_ID]
    }
    # validate that the missing checkpoint reported as
    # successfully removed
    assert res["result"] == expected_result

    res = backup.list_checkpoints(vm, dom)
    assert res["result"] == []


@requires_backup_support
def test_failed_delete_checkpoint():
    error_msg = "Internal delete error"

    dom = FakeDomainAdapter()

    checkpoint_2 = FakeCheckpoint(CHECKPOINT_2_XML, CHECKPOINT_2_ID, dom=dom)
    # simulating an error that raised when calling the delete method
    # of a specific checkpoint
    checkpoint_2.errors["delete"] = fake.libvirt_error(
        [libvirt.VIR_ERR_INTERNAL_ERROR, '', error_msg], "Fake libvirt error")

    dom.output_checkpoints = [
        FakeCheckpoint(CHECKPOINT_1_XML, CHECKPOINT_1_ID, dom=dom),
        checkpoint_2
    ]

    vm = FakeVm()
    res = backup.delete_checkpoints(
        vm, dom, [CHECKPOINT_1_ID, CHECKPOINT_2_ID])

    expected_result = {
        'checkpoint_ids': [CHECKPOINT_1_ID],
        'error': {
            'code': 1,
            'message': error_msg
        }
    }
    assert res["result"] == expected_result

    res = backup.list_checkpoints(vm, dom)
    assert res["result"] == [CHECKPOINT_2_ID]


@requires_backup_support
def test_dump_checkpoint():
    dom = FakeDomainAdapter()
    dom.output_checkpoints = [
        FakeCheckpoint(CHECKPOINT_1_XML, CHECKPOINT_1_ID),
        FakeCheckpoint(CHECKPOINT_2_XML, CHECKPOINT_2_ID)
    ]

    for checkpoint_cfg in FAKE_CHECKPOINT_CFG:
        res = backup.dump_checkpoint(dom, checkpoint_cfg['id'])

        expected_result = {
            'checkpoint': checkpoint_cfg['xml']
        }
        assert res["result"] == expected_result


@requires_backup_support
def test_dump_missing_checkpoint():
    dom = FakeDomainAdapter()
    dom.output_checkpoints = [
        FakeCheckpoint(CHECKPOINT_1_XML, CHECKPOINT_1_ID),
    ]

    with pytest.raises(exception.NoSuchCheckpointError):
        backup.dump_checkpoint(dom, CHECKPOINT_2_ID)


@requires_backup_support
def test_dump_checkpoint_lookup_failed():
    dom = FakeDomainAdapter()
    dom.errors["checkpointLookupByName"] = fake.libvirt_error(
        [libvirt.VIR_ERR_INTERNAL_ERROR], "Fake libvirt error")
    dom.output_checkpoints = [
        FakeCheckpoint(CHECKPOINT_1_XML, CHECKPOINT_1_ID),
        FakeCheckpoint(CHECKPOINT_2_XML, CHECKPOINT_2_ID)
    ]

    with pytest.raises(libvirt.libvirtError) as e:
        backup.dump_checkpoint(dom, CHECKPOINT_1_ID)
    assert e.value.get_error_code() == libvirt.VIR_ERR_INTERNAL_ERROR


@requires_backup_support
def test_dump_checkpoint_get_xml_failed():
    checkpoint_2 = FakeCheckpoint(CHECKPOINT_2_XML, CHECKPOINT_2_ID)
    # simulating an error that raised when calling the getXMLDesc method
    # of a specific checkpoint
    checkpoint_2.errors["getXMLDesc"] = fake.libvirt_error(
        [libvirt.VIR_ERR_INTERNAL_ERROR, '', 'Internal get XML error'],
        "Fake libvirt error")

    dom = FakeDomainAdapter()
    dom.output_checkpoints = [
        FakeCheckpoint(CHECKPOINT_1_XML, CHECKPOINT_1_ID),
        checkpoint_2
    ]

    with pytest.raises(libvirt.libvirtError) as e:
        backup.dump_checkpoint(dom, CHECKPOINT_2_ID)
    assert e.value.get_error_code() == libvirt.VIR_ERR_INTERNAL_ERROR


def verify_scratch_disks_exists(vm, backup_id=BACKUP_1_ID):
    res = vm.cif.irs.list_transient_disks(vm.id)
    assert res["status"]["code"] == 0

    scratch_disks = [backup_id + "." + drive.name
                     for drive in FAKE_DRIVES.values()]
    assert sorted(res["result"]) == sorted(scratch_disks)


def verify_backup_urls(backup_id, result_disks):
    for image_id, drive in FAKE_DRIVES.items():
        socket_path = backup.socket_path(backup_id)
        exp_addr = nbdutils.UnixAddress(socket_path).url(drive.name)
        assert result_disks[image_id] == exp_addr


def verify_scratch_disks_removed(vm):
    res = vm.cif.irs.list_transient_disks(vm.id)
    assert res['status']['code'] == 0
    assert res['result'] == []


def create_fake_disks(disks_in_checkpoint=(IMAGE_1_UUID, IMAGE_2_UUID)):
    fake_disks = []
    for img_id in FAKE_DRIVES:
        fake_disks.append({
            'domainID': DOMAIN_ID,
            'imageID': img_id,
            'volumeID': VOLUME_ID,
            'checkpoint': img_id in disks_in_checkpoint
        })
    return fake_disks


def _get_scratch_disks_path(backup_id):
    scratch_disk_paths = []
    for drive in FAKE_DRIVES.values():
        scratch_disk_name = backup_id + "." + drive.name
        scratch_disk_path = os.path.join(
            transientdisk.P_TRANSIENT_DISKS, "vm_id", scratch_disk_name)
        scratch_disk_paths.append(scratch_disk_path)

    return scratch_disk_paths
