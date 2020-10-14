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

import libvirt

from vdsm.common import exception
from vdsm.common import xmlutils

from vdsm.virt import vmxml
from vdsm.virt.vmdevices import drivename


class VolumeError(RuntimeError):
    def __str__(self):
        return "Bad volume specification " + RuntimeError.__str__(self)


def change_cd(vm, cdromspec):
    drivespec = cdromspec['path']
    blockdev = drivename.make(cdromspec['iface'], cdromspec['index'])
    iface = cdromspec['iface']
    return _change_block_dev(
        vm, 'cdrom', blockdev, drivespec, iface, force=bool(drivespec))


def change_floppy(vm, drivespec):
    return _change_block_dev(vm, 'floppy', 'fda', drivespec)


def _change_block_dev(vm, vmDev, blockdev, drivespec, iface=None, force=True):
    try:
        path = vm.cif.prepareVolumePath(drivespec)
    except VolumeError:
        raise exception.ImageFileNotFound()

    diskelem = vmxml.Element('disk', type='file', device=vmDev)
    diskelem.appendChildWithArgs('source', file=path)

    target = {'dev': blockdev}
    if iface:
        target['bus'] = iface

    diskelem.appendChildWithArgs('target', **target)
    diskelem_xml = xmlutils.tostring(diskelem)

    vm.log.info("changeBlockDev: using disk XML: %s", diskelem_xml)

    changed = False
    if not force:
        try:
            vm.domain().updateDeviceFlags(diskelem_xml)
        except libvirt.libvirtError:
            vm.log.info("regular updateDeviceFlags failed")
        else:
            changed = True

    if not changed:
        try:
            vm.domain().updateDeviceFlags(
                diskelem_xml, libvirt.VIR_DOMAIN_DEVICE_MODIFY_FORCE
            )
        except libvirt.libvirtError:
            vm.log.exception("forceful updateDeviceFlags failed")
            vm.cif.teardownVolumePath(drivespec)
            raise exception.ChangeDiskFailed()

    if vmDev in vm.conf:
        vm.cif.teardownVolumePath(vm.conf[vmDev])

    return {'vmList': {}}
