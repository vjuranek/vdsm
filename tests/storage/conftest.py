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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#

"""
Common fixtures that can be used without importing anything.
"""

from __future__ import absolute_import
from __future__ import division

import logging
import sys
import types

import pytest

from vdsm import jobs
from vdsm.storage import blockSD
from vdsm.storage import clusterlock
from vdsm.storage import fallocate
from vdsm.storage import fileVolume
from vdsm.storage import xlease
from vdsm.storage.task import Recovery

import fakelib

from .fakesanlock import FakeSanlock

log = logging.getLogger("test")


@pytest.fixture
def fake_sanlock(monkeypatch):
    """
    Create fake sanlock which mimics sanlock functionality.
    """
    fs = FakeSanlock()
    monkeypatch.setattr(clusterlock, "sanlock", fs)
    monkeypatch.setattr(blockSD, "sanlock", fs)
    monkeypatch.setattr(fileVolume, "sanlock", fs)
    monkeypatch.setattr(xlease, "sanlock", fs)
    return fs


@pytest.fixture
def local_fallocate(monkeypatch):
    monkeypatch.setattr(fallocate, '_FALLOCATE', '../helpers/fallocate')


@pytest.fixture
def fake_scheduler():
    scheduler = fakelib.FakeScheduler()
    notifier = fakelib.FakeNotifier()
    jobs.start(scheduler, notifier)
    yield
    jobs._clear()


@pytest.fixture
def add_recovery(monkeypatch):
    def add_recovery_func(task, module_name, params):
        class FakeRecovery(object):
            task_proxy = None
            args = None

            @classmethod
            def call(cls, task_proxy, *args):
                cls.task_proxy = task_proxy
                cls.args = args

        # Create a recovery module with the passed module name
        module = types.ModuleType(module_name)
        module.FakeRecovery = FakeRecovery

        # Verify that the fully qualified name of the module is unique
        full_name = "vdsm.storage.{}".format(module_name)
        if full_name in sys.modules:
            raise RuntimeError("Module {} already exists".format(module_name))

        # Set task's recovery lookup to refer to our local Recovery class
        monkeypatch.setattr(full_name, module, raising=False)
        monkeypatch.setitem(sys.modules, full_name, module)

        r = Recovery(module_name, module_name, "FakeRecovery", "call", params)
        task.pushRecovery(r)

        return FakeRecovery

    return add_recovery_func


@pytest.fixture
def fake_executable(tmpdir):
    """
    Prepares shell script which can be used by another fixture to fake a binary
    that is called in the test. Typical usage is to fake the binary output in
    the script.
    """
    path = tmpdir.join("fake-executable")
    path.ensure()
    path.chmod(0o755)

    return path
