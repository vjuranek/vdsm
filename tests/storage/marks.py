#
# Copyright 2019 Red Hat, Inc.
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

import os

import six
import pytest

from vdsm.common import cache
from vdsm.common import commands
from vdsm.common import compat

from vdsm.storage.compat import sanlock


requires_root = pytest.mark.skipif(
    os.geteuid() != 0, reason="requires root")

requires_sanlock_python3 = pytest.mark.skipif(
    isinstance(sanlock, compat.MissingModule),
    reason="requires sanlock for python 3")

xfail_python3 = pytest.mark.xfail(
    six.PY3, reason="needs porting to python 3")


@cache.memoized
def has_loopback_sector_size():
    out = commands.run(["losetup", "-h"])
    return "--sector-size <num>" in out.decode()


requires_loopback_sector_size = pytest.mark.skipif(
    not has_loopback_sector_size(),
    reason="lossetup --sector-size option not available")
