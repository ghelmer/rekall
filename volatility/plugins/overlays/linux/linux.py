# Volatility
# Copyright (C) 2010 Brendan Dolan-Gavitt
# Copyright (c) 2011 Michael Cohen <scudette@gmail.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or (at
# your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA
#

"""
@author:       Brendan Dolan-Gavitt
@license:      GNU General Public License 2.0 or later
@contact:      brendandg@gatech.edu
@organization: Georgia Institute of Technology
"""
import logging
import json
import re
import sys
import zipfile
import StringIO

from volatility import obj
from volatility import plugin

from volatility.plugins.overlays import basic
from volatility.plugins.overlays.linux import dwarfdump

# Try to use the elftools directly if they are present.
try:
    from volatility.plugins.overlays.linux import dwarfparser
    logging.info("Unable to load the dwarfparser module. Do you have "
                 "elftools installed?")
except ImportError:
    dwarfparser = None

linux_overlay = {
    'task_struct' : [None, {
        'comm'          : [ None , ['UnicodeString', dict(length = 16)]],
        }],
    'module'      : [None, {
        'name'          : [ None , ['UnicodeString', dict(length = 60)]],
        }],
    'super_block' : [None, {
        's_id'          : [ None , ['UnicodeString', dict(length = 32)]],
        }],
    'net_device'  : [None, {
        'name'          : [ None , ['UnicodeString', dict(length = 16)]],
        }],
    'sockaddr_un' : [None, {
        'sun_path'      : [ None , ['UnicodeString', dict(length =108)]],
        }],
    'cpuinfo_x86' : [None, {
        'x86_model_id'  : [ None , ['UnicodeString', dict(length = 64)]],
        'x86_vendor_id' : [ None,  ['UnicodeString', dict(length = 16)]],
        }],
    }


# really 'file' but don't want to mess with python's version
class linux_file(obj.CType):

    def get_dentry(self):
        if hasattr(self, "f_dentry"):
            ret = self.f_dentry
        else:
            ret = self.f_path.dentry

        return ret

    def get_vfsmnt(self):
        if hasattr(self, "f_vfsmnt"):
            ret = self.f_vfsmnt
        else:
            ret = self.f_path.mnt

        return ret


class list_head(basic.ListMixIn, obj.CType):
    """A list_head makes a doubly linked list."""
    _forward = "next"
    _backward = "prev"


class files_struct(obj.CType):

    def get_fds(self):
        if hasattr(self, "fdt"):
            fdt = self.fdt
            ret = fdt.fd.dereference()
        else:
            ret = self.fd.dereference()

        return ret

    def get_max_fds(self):
        if hasattr(self, "fdt"):
            ret = self.fdt.max_fds
        else:
            ret = self.max_fds

        return ret


class task_struct(obj.CType):

    @property
    def uid(self):
        ret = self.members.get("uid")
        if ret is None:
            ret = self.cred.uid

        return ret

    @property
    def gid(self):
        ret = self.members.get("gid")
        if ret is None:
            ret = self.cred.gid

        return ret

    @property
    def euid(self):
        ret = self.members.get("euid")
        if ret is None:
            ret = self.cred.euid

        return ret

    def get_process_address_space(self):
        directory_table_base = self.obj_vm.vtop(self.mm.pgd.v())

        try:
            process_as = self.obj_vm.__class__(
                base=self.obj_vm.base, session=self.obj_vm.session,
                dtb = directory_table_base)

        except AssertionError, _e:
            return obj.NoneObject("Unable to get process AS")

        process_as.name = "Process {0}".format(self.pid)

        return process_as


class linux_fs_struct(obj.CType):

    def get_root_dentry(self):
        # < 2.6.26
        if hasattr(self, "rootmnt"):
            ret = self.root
        else:
            ret = self.root.dentry

        return ret

    def get_root_mnt(self):
        # < 2.6.26
        if hasattr(self, "rootmnt"):
            ret = self.rootmnt
        else:
            ret = self.root.mnt

        return ret


class timespec(obj.CType):
    # The following calculate the number of ns each tick is.
    # http://lxr.free-electrons.com/source/include/linux/jiffies.h?v=2.6.32#L12

    # The HZ value should be obtained from the auxilary vector but for now we
    # hard code it. TODO: http://lwn.net/Articles/519085/
    HZ = 1000

    # The clock frequency of the i8253/i8254 PIT
    CLOCK_TICK_RATE = PIT_TICK_RATE = 1193182

    # LATCH is used in the interval timer and ftape setup.
    LATCH = ((CLOCK_TICK_RATE + HZ/2) / HZ)

    # HZ is the requested value. ACTHZ is actual HZ
    ACTHZ = (CLOCK_TICK_RATE / LATCH)

    # TICK_NSEC is the time between ticks in nsec assuming real ACTHZ
    TICK_NSEC = 1000000 * 1000 /  ACTHZ

    NSEC_PER_SEC = 1000000000

    @property
    def wall_to_monotonic(self):
        wall_addr = self.obj_profile.get_constant("wall_to_monotonic")
        if wall_addr:
            return self.obj_profile.timespec(vm=self.obj_vm, offset=wall_addr)

        # After Kernel 3.3 wall_to_monotonic is stored inside the timekeeper.
        timekeeper_addr = self.obj_profile.get_constant("timekeeper")
        if timekeeper_addr:
            return  self.obj_profile.timekeeper(
                vm=self.obj_vm, offset=timekeeper_addr).wall_to_monotonic

    @property
    def total_sleep_time(self):
        total_sleep_time_addr = self.obj_profile.get_constant("total_sleep_time")
        if total_sleep_time_addr:
            return self.obj_profile.timespec(
                vm=self.obj_vm, offset=total_sleep_time)

        # After Kernel 3.3 wall_to_monotonic is stored inside the timekeeper.
        timekeeper_addr = self.obj_profile.get_constant("timekeeper")
        if timekeeper_addr:
            return  self.obj_profile.timekeeper(
                vm=self.obj_vm, offset=timekeeper_addr).total_sleep_time

        # Just return an empty timespec.
        return self.obj_profile.timespec()

    def __add__(self, other):
        """Properly normalize this object from sec and nsec.

        based on set_normalized_timespec function.
        """
        if not isinstance(other, self.__class__):
            raise TypeError("Can only add timespec to timespec")

        sec = other.tv_sec + self.tv_sec
        nsec = other.tv_nsec + self.tv_nsec

        sec += nsec / self.NSEC_PER_SEC
        nsec = nsec % self.NSEC_PER_SEC

        result = self.obj_profile.timespec()
        result.tv_sec = sec
        result.tv_nsec = nsec

        return result

    def getboottime(self):
        result = self.wall_to_monotonic + self.total_sleep_time
        return result.tv_sec + result.tv_nsec / self.NSEC_PER_SEC

    def as_timestamp(self):
        secs = self.tv_sec + self.tv_nsec / self.NSEC_PER_SEC
        return self.obj_profile.UnixTimeStamp(value=secs)

class vm_area_struct(obj.CType):
    def __iter__(self):
        mmap = self
        while mmap:
            yield mmap

            mmap = mmap.vm_next.deref()



class Linux32(basic.Profile32Bits, basic.BasicWindowsClasses):
    """A Linux profile which works with dwarfdump output files.

    To generate a suitable dwarf file:
    dwarfdump -di vmlinux > output.dwarf
    """
    _md_os = "linux"
    _md_memory_model = "32bit"
    _md_type = "Kernel"

    def __init__(self, profile_file=None, **kwargs):
        super(Linux32, self).__init__(**kwargs)
        self.profile_file = profile_file
        self.add_classes(dict(
                file=linux_file, list_head=list_head,
                files_struct=files_struct, task_struct=task_struct,
                fs_struct=linux_fs_struct, timespec=timespec,
                vm_area_struct=vm_area_struct,
                ))
        self.add_overlay(linux_overlay)
        self.add_constants(default_text_encoding="utf8")

    def compile(self):
        """Delay checking the profile as much as possible.

        This allows the user to set the profile file after setting the profile.
        """
        profile_file = self.profile_file or self.session.profile_file
        if not profile_file:
            raise obj.ProfileError("No profile dwarf pack specified (session.profile_file).")

        self.parse_profile_file(profile_file)
        super(Linux32, self).compile()

    def _match_filename(self, regex, profile_zipfile):
        """A generator of filenames from the zip file which match the regex."""
        for f in profile_zipfile.namelist():
            if re.search(regex, f, re.I):
                yield f

    def load_vtypes(self, profile_zipfile):
        """Try to load vtypes from the zipfile in order of priority."""
        # First try to use any json file.
        for json_file in self._match_filename("\\.json$", profile_zipfile):
            logging.info("Found json file %s" % json_file)
            return json.loads(profile_zipfile.read(json_file))

        # We try to find the kernel module.
        if dwarfparser:
            for module_file in self._match_filename("\\.ko$", profile_zipfile):
                module = StringIO.StringIO(profile_zipfile.read(module_file))
                parser = dwarfparser.DWARFParser(module)
                result = parser.VType()
                if result:
                    logging.info("Found module file %s" % module_file)
                    return result

        # Failing this we try to parse dwarfdump output - note this is
        # deprecated. Currently only fairly old versions of dwarfdump actually
        # work.
        for dwarf_file in  self._match_filename("\\.dwarf$", profile_zipfile):
            logging.info("Found dwarfdump file %s" % dwarf_file)
            return self.parse_dwarf_from_dump(profile_zipfile.read(dwarf_file))

        # This is dangerous and is currently disabled.
        if 0:
            for vtype_file in  self._match_filename("\\.vtype$", profile_zipfile):
                logging.info("Found vtype file %s" % vtype_file)
                env = {}
                exec(profile_zipfile.read(vtype_file), dict(__builtins__=None), env)
                return env["linux_types"]

    def parse_profile_file(self, filename):
        """Parse the profile file into vtypes."""
        profile_file = zipfile.ZipFile(filename)
        vtypes = self.load_vtypes(profile_file)
        sys_map = None

        for f in self._match_filename("system.map", profile_file):
            logging.info("Found system map file %s" % f)
            sys_map = self.parse_system_map(profile_file.read(f))
            self.add_constants(**sys_map)

        if sys_map is None or vtypes is None:
            raise obj.ProfileError("DWARF profile file does not contain all required"
                                   " components.")

        self.add_types(vtypes)

    def parse_dwarf_from_dump(self, data):
        """Parse the dwarf file."""
        self._parser = dwarfdump.DWARFParser()
        for line in data.splitlines():
            self._parser.feed_line(line)

        return self._parser.finalize()

    def parse_system_map(self, data):
        """Parse the symbol file."""
        sys_map = {}
        # get the system map
        for line in data.splitlines():
            (address, _, symbol) = line.strip().split()
            try:
                sys_map[symbol] = long(address, 16)
            except ValueError:
                pass

        return sys_map


class Linux64(basic.ProfileLP64, Linux32):
    """Support for 64 bit linux systems."""
