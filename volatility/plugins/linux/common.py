# Volatility
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

"""
@author:       Andrew Case
@license:      GNU General Public License 2.0 or later
@contact:      atcuno@gmail.com
@organization: Digital Forensics Solutions
"""

import volatility.commands as commands
import volatility.utils as utils
import volatility.debug as debug
import volatility.obj as obj
import volatility.plugins.linux.flags as linux_flags

MAX_STRING_LENGTH = 256

import time
nsecs_per = 1000000000

def set_plugin_members(obj_ref):

    obj_ref.addr_space = utils.load_as(obj_ref._config)

class AbstractLinuxCommand(commands.Command):

    def __init__(self, *args, **kwargs):
        self.addr_space = None
        commands.Command.__init__(self, *args, **kwargs)

    @property
    def profile(self):
        if self.addr_space:
            return self.addr_space.profile
        return None

    def execute(self, *args, **kwargs):
        commands.Command.execute(self, *args, **kwargs)

    @staticmethod
    def is_valid_profile(profile):
        return profile.metadata.get('os', 'Unknown').lower() == 'linux'

    def get_profile_symbol(self, sym_name, nm_type = "", sym_type = "", module = "kernel"):
        '''
        Gets a symbol out of the profile
        syn_name -> name of the symbol
        nm_tyes  -> types as defined by 'nm' (man nm for examples)
        sym_type -> the type of the symbol (passing Pointer will provide auto deref)
        module   -> which module to get the symbol from, default is kernel, otherwise can be any name seen in 'lsmod'

        Just a wrapper for AbstractLinuxProfile.get_symbol
        '''
        return self.profile.get_symbol(sym_name, nm_type, sym_type, module)

    # In 2.6.3x, Linux changed how the symbols for per_cpu variables were named
    # This handles both formats so plugins needing per-cpu vars are cleaner
    def get_per_cpu_symbol(self, sym_name, module = "kernel"):

        ret = self.get_profile_symbol(sym_name, module = module)

        if not ret:
            ret = self.get_profile_symbol("per_cpu__" + sym_name, module = module)

        return ret

    ## FIXME: This currently returns using localtime, we should probably use UTC?
    def get_task_start_time(self, task):

        start_time = task.start_time

        start_secs = start_time.tv_sec + (start_time.tv_nsec / nsecs_per / 100)

        sec = get_boot_time(self) + start_secs

        # protect against invalid data in unallocated tasks
        try:
            ret = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.localtime(sec))
        except ValueError:
            ret = ""

        return ret

# returns a list of online cpus (the processor numbers)
def online_cpus(self):

    cpu_online_bits_addr = self.get_profile_symbol("cpu_online_bits")
    cpu_present_map_addr = self.get_profile_symbol("cpu_present_map")

    #later kernels..
    if cpu_online_bits_addr:
        bmap = obj.Object("unsigned long", offset = cpu_online_bits_addr, vm = self.addr_space)

    elif cpu_present_map_addr:
        bmap = obj.Object("unsigned long", offset = cpu_present_map_addr, vm = self.addr_space)

    else:
        raise AttributeError, "Unable to determine number of online CPUs for memory capture"

    cpus = []
    for i in range(8):
        if bmap & (1 << i):
            cpus.append(i)

    return cpus

def walk_per_cpu_var(obj_ref, per_var, var_type):

    cpus = online_cpus(obj_ref)

    # get the highest numbered cpu
    max_cpu = cpus[-1] + 1

    offset_var = obj_ref.get_profile_symbol("__per_cpu_offset")
    per_offsets = obj.Object(theType = 'Array', targetType = 'unsigned long', count = max_cpu, offset = offset_var, vm = obj_ref.addr_space)

    for i in range(max_cpu):

        offset = per_offsets[i]

        cpu_var = obj_ref.get_per_cpu_symbol(per_var)

        addr = cpu_var + offset.v()
        var = obj.Object(var_type, offset = addr, vm = obj_ref.addr_space)

        yield i, var

def get_time_vars(obj_ref):
    '''
    Sometime in 3.[3-5], Linux switched to a global timekeeper structure
    This just figures out which is in use and returns the correct variables
    '''

    wall_addr = obj_ref.get_profile_symbol("wall_to_monotonic")

    # old way
    if wall_addr:
        wall = obj.Object("timespec", offset = wall_addr, vm = obj_ref.addr_space)

        sleep_addr = obj_ref.get_profile_symbol("total_sleep_time")
        timeo = obj.Object("timespec", offset = sleep_addr, vm = obj_ref.addr_space)

    # timekeeper way
    else:
        timekeeper_addr = obj_ref.get_profile_symbol("timekeeper")

        timekeeper = obj.Object("timekeeper", offset = timekeeper_addr, vm = obj_ref.addr_space)

        wall = timekeeper.wall_to_monotonic
        timeo = timekeeper.total_sleep_time

    return (wall, timeo)

# based on 2.6.35 getboottime
def get_boot_time(obj_ref):

    (wall, timeo) = get_time_vars(obj_ref)

    secs = wall.tv_sec + timeo.tv_sec
    nsecs = wall.tv_nsec + timeo.tv_nsec

    secs = secs * -1
    nsecs = nsecs * -1

    while nsecs >= nsecs_per:

        nsecs = nsecs - nsecs_per

        secs = secs + 1

    while nsecs < 0:

        nsecs = nsecs + nsecs_per

        secs = secs - 1

    boot_time = secs + (nsecs / nsecs_per / 100)

    return boot_time


# similar to for_each_process for this usage
def walk_list_head(struct_name, list_member, list_head_ptr, _addr_space):
    debug.warning("Deprecated use of walk_list_head")

    for item in list_head_ptr.list_of_type(struct_name, list_member):
        yield item

def walk_internal_list(struct_name, list_member, list_start, addr_space = None):
    if not addr_space:
        addr_space = list_start.obj_vm

    while list_start:
        list_struct = obj.Object(struct_name, vm = addr_space, offset = list_start.v())
        yield list_struct
        list_start = getattr(list_struct, list_member)


# based on __d_path
# TODO: (deleted) support
def do_get_path(rdentry, rmnt, dentry, vfsmnt):
    ret_path = []

    inode = dentry.d_inode

    if not rdentry.is_valid() or not dentry.is_valid():
        return []

    while (dentry != rdentry or vfsmnt != rmnt) and dentry.d_name.name.is_valid():

        dname = dentry.d_name.name.dereference_as("String", length = MAX_STRING_LENGTH)

        ret_path.append(dname.strip('/'))

        if dentry == vfsmnt.mnt_root or dentry == dentry.d_parent:
            if vfsmnt.mnt_parent == vfsmnt.v():
                break
            dentry = vfsmnt.mnt_mountpoint
            vfsmnt = vfsmnt.mnt_parent
            continue

        parent = dentry.d_parent
        dentry = parent

    ret_path.reverse()

    if ret_path == []:
        return []

    ret_val = '/'.join([str(p) for p in ret_path if p != ""])

    if ret_val.startswith(("socket:", "pipe:")):
        if ret_val.find("]") == -1:
            ret_val = ret_val[:-1] + ":[{0}]".format(inode.i_ino)
        else:
            ret_val = ret_val.replace("/", "")

    elif ret_val != "inotify":
        ret_val = '/' + ret_val

    return ret_val

def get_path(task, filp):
    rdentry = task.fs.get_root_dentry()
    rmnt = task.fs.get_root_mnt()
    dentry = filp.dentry
    vfsmnt = filp.vfsmnt

    return do_get_path(rdentry, rmnt, dentry, vfsmnt)

def get_obj(self, ptr, sname, member):

    offset = self.profile.get_obj_offset(sname, member)

    addr = ptr - offset

    return obj.Object(sname, offset = addr, vm = self.addr_space)

def S_ISDIR(mode):
    return (mode & linux_flags.S_IFMT) == linux_flags.S_IFDIR

def S_ISREG(mode):
    return (mode & linux_flags.S_IFMT) == linux_flags.S_IFREG

###################
# code to walk the page cache and mem_map / mem_section page structs
###################
# FIXME - use 'class page' overlay?
def phys_addr_of_page(self, page):

    mem_map_addr = self.get_profile_symbol("mem_map")
    mem_section_addr = self.get_profile_symbol("mem_section")

    if mem_map_addr:
        # FLATMEM kernels, usually 32 bit
        mem_map_ptr = obj.Object("Pointer", offset = mem_map_addr, vm = self.addr_space)

    elif mem_section_addr:
        # this is hardcoded in the kernel - VMEMMAPSTART, usually 64 bit kernels
        # NOTE: This is really 0xffff0xea0000000000 but we chop to its 48 bit equivalent
        # FIXME: change in 2.3 when truncation no longer occurs
        mem_map_ptr = 0xea0000000000

    else:
        debug.error("phys_addr_of_page: Unable to determine physical address of page\n")

    phys_offset = (page - mem_map_ptr) / self.profile.get_obj_size("page")

    phys_offset = phys_offset << 12

    return phys_offset

def radix_tree_is_indirect_ptr(self, ptr):

    return ptr & 1

def radix_tree_indirect_to_ptr(self, ptr):

    return obj.Object("radix_tree_node", offset = ptr & ~1, vm = self.addr_space)

def radix_tree_lookup_slot(self, root, index):

    self.RADIX_TREE_MAP_SHIFT = 6
    self.RADIX_TREE_MAP_SIZE = 1 << self.RADIX_TREE_MAP_SHIFT
    self.RADIX_TREE_MAP_MASK = self.RADIX_TREE_MAP_SIZE - 1

    node = root.rnode

    if radix_tree_is_indirect_ptr(self, node) == 0:

        if index > 0:
            #print "returning None: index > 0"
            return None

        #print "returning obj_Offset"
        off = root.obj_offset + self.profile.get_obj_offset("radix_tree_root", "rnode")

        page = obj.Object("Pointer", offset = off, vm = self.addr_space)

        return page

    node = radix_tree_indirect_to_ptr(self, node)

    height = node.height

    shift = (height - 1) * self.RADIX_TREE_MAP_SHIFT

    slot = -1

    while 1:

        idx = (index >> shift) & self.RADIX_TREE_MAP_MASK

        slot = node.slots[idx]

        shift = shift - self.RADIX_TREE_MAP_SHIFT

        height = height - 1

        if height <= 0:
            break

    if slot == -1:
        return None

    return slot

def SHMEM_I(self, inode):

    offset = self.profile.get_obj_offset("shmem_inode_info", "vfs_inode")

    return obj.Object("shmem_inode_info", offset = inode.obj_offset - offset, vm = self.addr_space)

def find_get_page(self, inode, offset):

    page = radix_tree_lookup_slot(self, inode.i_mapping.page_tree, offset)

    #if not page:
        # TODO swapper_space support
        #print "no page"

    return page

def get_page_contents(self, inode, idx):

    page = find_get_page(self, inode, idx)

    if page:
        #print "inode: %lx | %lx page: %lx" % (inode, inode.v(), page)

        phys_offset = phys_addr_of_page(self, page)

        phys_as = utils.load_as(self._config, astype = 'physical')

        data = phys_as.read(phys_offset, 4096)
    else:
        data = "\x00" * 4096

    return data

# main function to be called, handles getting all the pages of an inode
# and handles the last page not being page_size aligned 
def get_file_contents(self, inode):

    data = ""
    file_size = inode.i_size

    extra = file_size % 4096

    idxs = file_size / 4096

    if extra != 0:
        extra = 4096 - extra
        idxs = idxs + 1

    for idx in range(0, idxs):

        data = data + get_page_contents(self, inode, idx)

    # this is chop off any extra data on the last page

    if extra != 0:
        extra = extra * -1

        data = data[:extra]

    return data

def is_known_address(obj_ref, addr, modules):

    text = obj_ref.profile.get_symbol("_text", sym_type = "Pointer")
    etext = obj_ref.profile.get_symbol("_etext", sym_type = "Pointer")

    if text <= addr < etext or address_in_module(modules, addr):
        known = 1
    else:
        known = 0

    return known

# This returns the name of the module that contains an address or None
# The module_list parameter comes from a call to get_modules
# This function will be updated after 2.2 to resolve symbols within the module as well
def address_in_module(module_list, address):
    ret = None

    for (name, start, end) in module_list:

        if start <= address < end:

            ret = name
            break

    return ret

def verify_ops(obj_ref, fops, op_members, modules):

    for check in op_members:
        addr = fops.m(check)

        if addr and addr != 0:

            if addr in obj_ref.known_addrs:
                known = obj_ref.known_addrs[addr]
            else:
                known = is_known_address(obj_ref, addr, modules)
                obj_ref.known_addrs[addr] = known

            if known == 0:
                yield (check, addr)

# we can't get the full path b/c we 
# do not have a ref to the vfsmnt
def get_partial_path(dentry):
    path = []

    name = ""

    while dentry and dentry != dentry.d_parent:
        name = dentry.d_name.name.dereference_as("String", length = 255)
        if name.is_valid():
            path.append(str(name))
        dentry = dentry.d_parent

    path.reverse()

    str_path = "/".join([p for p in path])

    return str_path

