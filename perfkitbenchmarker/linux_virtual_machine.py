# Copyright 2019 PerfKitBenchmarker Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Module containing mixin classes for linux virtual machines.

These classes allow installation on both Debian and RHEL based linuxes.
They also handle some initial setup (especially on RHEL based linuxes
since by default sudo commands without a tty don't work) and
can restore the VM to the state it was in before packages were
installed.

To install a package on a VM, just call vm.Install(package_name).
The package name is just the name of the package module (i.e. the
file name minus .py). The framework will take care of all cleanup
for you.
"""

import abc
import collections
import copy
# import functools
import logging
import os
import pipes
import posixpath
import re
import threading
import time
from typing import Optional, Dict, Set
import uuid

from absl import flags
from perfkitbenchmarker import context
from perfkitbenchmarker import disk
from perfkitbenchmarker import errors
from perfkitbenchmarker import linux_packages
from perfkitbenchmarker import os_types
from perfkitbenchmarker import regex_util
from perfkitbenchmarker import virtual_machine
from perfkitbenchmarker import vm_util

import yaml

FLAGS = flags.FLAGS


OS_PRETTY_NAME_REGEXP = r'PRETTY_NAME="(.*)"'
_EPEL_URL = 'https://dl.fedoraproject.org/pub/epel/epel-release-latest-{}.noarch.rpm'
CLEAR_BUILD_REGEXP = r'Installed version:\s*(.*)\s*'
UPDATE_RETRIES = 5
DEFAULT_SSH_PORT = 22
REMOTE_KEY_PATH = '~/.ssh/id_rsa'
CONTAINER_MOUNT_DIR = '/mnt'
CONTAINER_WORK_DIR = '/root'

# This pair of scripts used for executing long-running commands, which will be
# resilient in the face of SSH connection errors.
# EXECUTE_COMMAND runs a command, streaming stdout / stderr to a file, then
# writing the return code to a file. An exclusive lock is acquired on the return
# code file, so that other processes may wait for completion.
EXECUTE_COMMAND = 'execute_command.py'
# WAIT_FOR_COMMAND waits on the file lock created by EXECUTE_COMMAND,
# then copies the stdout and stderr, exiting with the status of the command run
# by EXECUTE_COMMAND.
WAIT_FOR_COMMAND = 'wait_for_command.py'

_DEFAULT_DISK_FS_TYPE = 'ext4'
_DEFAULT_DISK_MOUNT_OPTIONS = 'discard'
_DEFAULT_DISK_FSTAB_OPTIONS = 'defaults'

# regex for parsing lscpu and /proc/cpuinfo
_COLON_SEPARATED_RE = re.compile(r'^\s*(?P<key>.*?)\s*:\s*(?P<value>.*?)\s*$')

flags.DEFINE_bool('setup_remote_firewall', False,
                  'Whether PKB should configure the firewall of each remote'
                  'VM to make sure it accepts all internal connections.')

flags.DEFINE_list('sysctl', [],
                  'Sysctl values to set. This flag should be a comma-separated '
                  'list of path=value pairs. Each pair will be appended to'
                  '/etc/sysctl.conf.  The presence of any items in this list '
                  'will cause a reboot to occur after VM prepare. '
                  'For example, if you pass '
                  '--sysctls=vm.dirty_background_ratio=10,vm.dirty_ratio=25, '
                  'PKB will append "vm.dirty_background_ratio=10" and'
                  '"vm.dirty_ratio=25" on separate lines to /etc/sysctrl.conf'
                  ' and then the machine will be rebooted before starting'
                  'the benchmark.')

flags.DEFINE_list(
    'set_files',
    [],
    'Arbitrary filesystem configuration. This flag should be a '
    'comma-separated list of path=value pairs. Each value will '
    'be written to the corresponding path. For example, if you '
    'pass --set_files=/sys/kernel/mm/transparent_hugepage/enabled=always, '
    'then PKB will write "always" to '
    '/sys/kernel/mm/transparent_hugepage/enabled before starting '
    'the benchmark.')

flags.DEFINE_bool('network_enable_BBR', False,
                  'A shortcut to enable BBR congestion control on the network. '
                  'equivalent to appending to --sysctls the following values '
                  '"net.core.default_qdisc=fq, '
                  '"net.ipv4.tcp_congestion_control=bbr" '
                  'As with other sysctrls, will cause a reboot to happen.')

flags.DEFINE_integer('num_disable_cpus', None,
                     'Number of CPUs to disable on the virtual machine.'
                     'If the VM has n CPUs, you can disable at most n-1.',
                     lower_bound=1)
flags.DEFINE_integer('disk_fill_size', 0,
                     'Size of file to create in GBs.')
flags.DEFINE_enum('disk_fs_type', _DEFAULT_DISK_FS_TYPE,
                  [_DEFAULT_DISK_FS_TYPE, 'xfs'],
                  'File system type used to format disk.')
flags.DEFINE_integer(
    'disk_block_size', None, 'Block size to format disk with.'
    'Defaults to 4096 for ext4.')

flags.DEFINE_bool(
    'enable_transparent_hugepages', None, 'Whether to enable or '
    'disable transparent hugepages. If unspecified, the setting '
    'is unchanged from the default in the OS.')

flags.DEFINE_integer(
    'ssh_retries', 10, 'Default number of times to retry SSH.', lower_bound=0)

flags.DEFINE_integer(
    'scp_connect_timeout', 30, 'timeout for SCP connection.', lower_bound=0)

flags.DEFINE_string(
    'append_kernel_command_line', None,
    'String to append to the kernel command line. The presence of any '
    'non-empty string will cause a reboot to occur after VM prepare. '
    'If unspecified, the kernel command line will be unmodified.')

flags.DEFINE_integer(
    'tcp_max_receive_buffer', None,
    'Changes the third component of the sysctl value net.ipv4.tcp_rmem. '
    'This sets the maximum receive buffer for TCP socket connections in bytes. '
    'Increasing this value may increase single stream TCP throughput '
    'for high latency connections')

flags.DEFINE_integer(
    'tcp_max_send_buffer', None,
    'Changes the third component of the sysctl value net.ipv4.tcp_wmem. '
    'This sets the maximum send buffer for TCP socket connections in bytes. '
    'Increasing this value may increase single stream TCP throughput '
    'for high latency connections')

flags.DEFINE_integer(
    'rmem_max', None,
    'Sets the sysctl value net.core.rmem_max. This sets the max OS '
    'receive buffer size in bytes for all types of connections')

flags.DEFINE_integer(
    'wmem_max', None,
    'Sets the sysctl value net.core.wmem_max. This sets the max OS '
    'send buffer size in bytes for all types of connections')

flags.DEFINE_boolean('gce_hpc_tools', False,
                     'Whether to apply the hpc-tools environment script.')

flags.DEFINE_boolean('disable_smt', False,
                     'Whether to disable SMT (Simultaneous Multithreading) '
                     'in BIOS.')

_DISABLE_YUM_CRON = flags.DEFINE_boolean(
    'disable_yum_cron', True, 'Whether to disable the cron-run yum service.')

RETRYABLE_SSH_RETCODE = 255


class CpuVulnerabilities:
  """The 3 different vulnerablity statuses from vm.cpu_vulernabilities.

  Example input:
    /sys/devices/system/cpu/vulnerabilities/itlb_multihit:KVM: Vulnerable
  Is put into vulnerability with a key of "itlb_multihit" and value "KVM"

  Unparsed lines are put into the unknown dict.
  """

  def __init__(self):
    self.mitigations: Dict[str, str] = {}
    self.vulnerabilities: Dict[str, str] = {}
    self.notaffecteds: Set[str] = set()
    self.unknowns: Dict[str, str] = {}

  def AddLine(self, full_line: str) -> None:
    """Parses a line of output from the cpu/vulnerabilities/* files."""
    if not full_line:
      return
    file_path, line = full_line.split(':', 1)
    file_name = posixpath.basename(file_path)
    if self._AddMitigation(file_name, line):
      return
    if self._AddVulnerability(file_name, line):
      return
    if self._AddNotAffected(file_name, line):
      return
    self.unknowns[file_name] = line

  def _AddMitigation(self, file_name, line):
    match = re.match('^Mitigation: (.*)', line) or re.match(
        '^([^:]+): Mitigation: (.*)$', line)
    if match:
      self.mitigations[file_name] = ':'.join(match.groups())
      return True

  def _AddVulnerability(self, file_name, line):
    match = re.match('^Vulnerable: (.*)', line) or re.match(
        '^Vulnerable$', line) or re.match('^([^:]+): Vulnerable$', line)
    if match:
      self.vulnerabilities[file_name] = ':'.join(match.groups())
      return True

  def _AddNotAffected(self, file_name, line):
    match = re.match('^Not affected$', line)
    if match:
      self.notaffecteds.add(file_name)
      return True

  @property
  def asdict(self) -> Dict[str, str]:
    """Returns the parsed CPU vulnerabilities as a dict."""
    ret = {}
    if self.mitigations:
      ret['mitigations'] = ','.join(sorted(self.mitigations))
      for key, value in self.mitigations.items():
        ret[f'mitigation_{key}'] = value
    if self.vulnerabilities:
      ret['vulnerabilities'] = ','.join(sorted(self.vulnerabilities))
      for key, value in self.vulnerabilities.items():
        ret[f'vulnerability_{key}'] = value
    if self.unknowns:
      ret['unknowns'] = ','.join(self.unknowns)
      for key, value in self.unknowns.items():
        ret[f'unknown_{key}'] = value
    if self.notaffecteds:
      ret['notaffecteds'] = ','.join(sorted(self.notaffecteds))
    return ret


class KernelRelease(object):
  """Holds the contents of the linux kernel version returned from uname -r."""

  def __init__(self, uname: str):
    """KernelVersion Constructor.

    Args:
      uname: A string in the format of "uname -r" command
    """

    # example format would be: "4.5.0-96-generic"
    # or "3.10.0-514.26.2.el7.x86_64" for centos
    # major.minor.Rest
    # in this example, major = 4, minor = 5
    self.name = uname
    major_string, minor_string, _ = uname.split('.', 2)
    self.major = int(major_string)
    self.minor = int(minor_string)

  def AtLeast(self, major, minor):
    """Check If the kernel version meets a minimum bar.

    The kernel version needs to be at least as high as the major.minor
    specified in args.

    Args:
      major: The major number to test, as an integer
      minor: The minor number to test, as an integer

    Returns:
      True if the kernel version is at least as high as major.minor,
      False otherwise
    """
    if self.major < major:
      return False
    if self.major > major:
      return True
    return self.minor >= minor

  def __repr__(self) -> str:
    return self.name


class BaseLinuxMixin(virtual_machine.BaseOsMixin):
  """Class that holds Linux related VM methods and attributes."""

  # If multiple ssh calls are made in parallel using -t it will mess
  # the stty settings up and the terminal will become very hard to use.
  # Serializing calls to ssh with the -t option fixes the problem.
  _pseudo_tty_lock = threading.Lock()

  # TODO(user): Remove all uses of Python 2.
  PYTHON_2_PACKAGE = 'python2'

  # this command might change depending on the OS, but most linux distributions
  # can use the following command
  INIT_RAM_FS_CMD = 'sudo update-initramfs -u'

  # regex to get the network devices from "ip link show"
  _IP_LINK_RE_DEVICE_MTU = re.compile(
      r'^\d+: (?P<device_name>\S+):.*mtu (?P<mtu>\d+)')
  # device prefixes to ignore from "ip link show"
  # TODO(spencerkim): Record ib device metadata.
  _IGNORE_NETWORK_DEVICE_PREFIXES = ('lo', 'docker', 'ib')

  def __init__(self, *args, **kwargs):
    super(BaseLinuxMixin, self).__init__(*args, **kwargs)
    # N.B. If you override ssh_port you must override remote_access_ports and
    # primary_remote_access_port.
    self.ssh_port = DEFAULT_SSH_PORT
    self.remote_access_ports = [self.ssh_port]
    self.primary_remote_access_port = self.ssh_port
    self.has_private_key = False

    self._remote_command_script_upload_lock = threading.Lock()
    self._has_remote_command_script = False
    self._needs_reboot = False
    self._lscpu_cache = None
    self._partition_table = {}
    self._proccpu_cache = None
    self._smp_affinity_script = None
    self._os_info: Optional[str] = None
    self._kernel_release: Optional[KernelRelease] = None
    self._cpu_arch: Optional[str] = None
    self._kernel_command_line: Optional[str] = None
    self._network_device_mtus = None

  def _Suspend(self):
    """Suspends a VM."""
    raise NotImplementedError()

  def _Resume(self):
    """Resumes a VM."""
    raise NotImplementedError()

  def _BeforeSuspend(self):
    pass

  def _CreateVmTmpDir(self):
    self.RemoteCommand('mkdir -p %s' % vm_util.VM_TMP_DIR)

  def _SetTransparentHugepages(self):
    """Sets transparent hugepages based on --enable_transparent_hugepages.

    If the flag is unset (None), this is a nop.
    """
    if FLAGS.enable_transparent_hugepages is None:
      return
    setting = 'always' if FLAGS.enable_transparent_hugepages else 'never'
    self.RemoteCommand(
        'echo %s | sudo tee /sys/kernel/mm/transparent_hugepage/enabled' %
        setting)
    self.os_metadata['transparent_hugepage'] = setting

  def _SetupRobustCommand(self):
    """Sets up the RobustRemoteCommand tooling.

    This includes installing python3 and pushing scripts required by
    RobustRemoteCommand to this VM.  There is a check to skip if previously
    installed.
    """
    with self._remote_command_script_upload_lock:
      if not self._has_remote_command_script:
        # Python3 is needed for RobustRemoteCommands
        self.Install('python3')

        for f in (EXECUTE_COMMAND, WAIT_FOR_COMMAND):
          remote_path = os.path.join(vm_util.VM_TMP_DIR, os.path.basename(f))
          if os.path.basename(remote_path):
            self.RemoteCommand('sudo rm -f ' + remote_path)
          self.PushDataFile(f, remote_path)
        self._has_remote_command_script = True

  def RobustRemoteCommand(self, command, should_log=False, timeout=None,
                          ignore_failure=False):
    """Runs a command on the VM in a more robust way than RemoteCommand.

    This is used for long-running commands that might experience network issues
    that would normally interrupt a RemoteCommand and fail to provide results.
    Executes a command via a pair of scripts on the VM:

    * EXECUTE_COMMAND, which runs 'command' in a nohupped background process.
    * WAIT_FOR_COMMAND, which first waits on confirmation that EXECUTE_COMMAND
      has acquired an exclusive lock on a file with the command's status. This
      is done by waiting for the existence of a file written by EXECUTE_COMMAND
      once it successfully acquires an exclusive lock. Once confirmed,
      WAIT_COMMAND waits to acquire the file lock held by EXECUTE_COMMAND until
      'command' completes, then returns with the stdout, stderr, and exit status
      of 'command'.

    Temporary SSH failures (where ssh returns a 255) while waiting for the
    command to complete will be tolerated and safely retried. However, if
    remote command actually returns 255, SSH will return 1 instead to bypass
    retry behavior.

    Args:
      command: The command to run.
      should_log: Whether to log the command's output at the info level. The
          output is always logged at the debug level.
      timeout: The timeout for the command in seconds.
      ignore_failure: Ignore any failure if set to true.

    Returns:
      A tuple of stdout, stderr from running the command.

    Raises:
      RemoteCommandError: If there was a problem establishing the connection, or
          the command fails.
    """
    self._SetupRobustCommand()

    execute_path = os.path.join(vm_util.VM_TMP_DIR,
                                os.path.basename(EXECUTE_COMMAND))
    wait_path = os.path.join(vm_util.VM_TMP_DIR,
                             os.path.basename(WAIT_FOR_COMMAND))

    uid = uuid.uuid4()
    file_base = os.path.join(vm_util.VM_TMP_DIR, 'cmd%s' % uid)
    wrapper_log = file_base + '.log'
    stdout_file = file_base + '.stdout'
    stderr_file = file_base + '.stderr'
    status_file = file_base + '.status'
    exclusive_file = file_base + '.exclusive'

    if not isinstance(command, str):
      command = ' '.join(command)

    start_command = ['nohup', 'python3', execute_path,
                     '--stdout', stdout_file,
                     '--stderr', stderr_file,
                     '--status', status_file,
                     '--exclusive', exclusive_file,
                     '--command', pipes.quote(command)]  # pyformat: disable
    if timeout:
      start_command.extend(['--timeout', str(timeout)])

    start_command = '%s 1> %s 2>&1 &' % (' '.join(start_command),
                                         wrapper_log)
    self.RemoteCommand(start_command)

    def _WaitForCommand():
      wait_command = ['python3', wait_path,
                      '--status', status_file,
                      '--exclusive', exclusive_file]  # pyformat: disable
      stdout = ''
      while 'Command finished.' not in stdout:
        stdout, _ = self.RemoteCommand(
            ' '.join(wait_command), should_log=should_log, timeout=1800)
      wait_command.extend([
          '--stdout', stdout_file,
          '--stderr', stderr_file,
          '--delete',
      ])  # pyformat: disable
      return self.RemoteCommand(' '.join(wait_command), should_log=should_log,
                                ignore_failure=ignore_failure)

    try:
      return _WaitForCommand()
    except errors.VirtualMachine.RemoteCommandError:
      # In case the error was with the wrapper script itself, print the log.
      stdout, _ = self.RemoteCommand('cat %s' % wrapper_log, should_log=False)
      if stdout.strip():
        logging.warning('Exception during RobustRemoteCommand. '
                        'Wrapper script log:\n%s', stdout)
      raise

  def SetupRemoteFirewall(self):
    """Sets up IP table configurations on the VM."""
    self.RemoteHostCommand('sudo iptables -A INPUT -j ACCEPT')
    self.RemoteHostCommand('sudo iptables -A OUTPUT -j ACCEPT')

  def SetupProxy(self):
    """Sets up proxy configuration variables for the cloud environment."""
    env_file = '/etc/environment'
    commands = []

    if FLAGS.http_proxy:
      commands.append("echo 'http_proxy=%s' | sudo tee -a %s" % (
          FLAGS.http_proxy, env_file))

    if FLAGS.https_proxy:
      commands.append("echo 'https_proxy=%s' | sudo tee -a %s" % (
          FLAGS.https_proxy, env_file))

    if FLAGS.ftp_proxy:
      commands.append("echo 'ftp_proxy=%s' | sudo tee -a %s" % (
          FLAGS.ftp_proxy, env_file))

    if commands:
      self.RemoteCommand(';'.join(commands))

  def SetupPackageManager(self):
    """Specific Linux flavors should override this."""
    pass

  def PrepareVMEnvironment(self):
    super(BaseLinuxMixin, self).PrepareVMEnvironment()
    self.SetupProxy()
    self._CreateVmTmpDir()
    self._SetTransparentHugepages()
    if FLAGS.setup_remote_firewall:
      self.SetupRemoteFirewall()
    if self.install_packages:
      self._CreateInstallDir()
      if self.is_static:
        self.SnapshotPackages()
      # TODO(user): Only setup if necessary.
      # Call SetupPackageManager lazily from HasPackage/InstallPackages like
      # ShouldDownloadPreprovisionedData sets up object storage CLIs.
      self.SetupPackageManager()
    self.SetFiles()
    self.DoSysctls()
    self._DoAppendKernelCommandLine()
    self.DoConfigureNetworkForBBR()
    self.DoConfigureTCPWindow()
    self.UpdateEnvironmentPath()
    self._DisableCpus()
    self._RebootIfNecessary()
    self.RecordAdditionalMetadata()
    self.BurnCpu()
    self.FillDisk()

  def _CreateInstallDir(self):
    self.RemoteCommand(
        ('sudo mkdir -p {0}; '
         'sudo chmod a+rwxt {0}').format(linux_packages.INSTALL_DIR))

  # LinuxMixins do not implement _Start or _Stop
  def _Start(self):
    """Starts the VM."""
    raise NotImplementedError()

  def _Stop(self):
    """Stops the VM."""
    raise NotImplementedError()

  def SetFiles(self):
    """Apply --set_files to the VM."""

    for pair in FLAGS.set_files:
      path, value = pair.split('=')
      self.RemoteCommand('echo "%s" | sudo tee %s' %
                         (value, path))

  def _DisableCpus(self):
    """Apply num_disable_cpus to the VM.

    Raises:
      ValueError: if num_disable_cpus is outside of (0 ... num_cpus-1)
                  inclusive
    """
    if not FLAGS.num_disable_cpus:
      return

    self.num_disable_cpus = FLAGS.num_disable_cpus

    if (self.num_disable_cpus <= 0 or
        self.num_disable_cpus >= self.num_cpus):
      raise ValueError('num_disable_cpus must be between 1 and '
                       '(num_cpus - 1) inclusive.  '
                       'num_disable_cpus: %i, num_cpus: %i' %
                       (self.num_disable_cpus, self.num_cpus))

    # We can't disable cpu 0, starting from the last cpu in /proc/cpuinfo.
    # On multiprocessor systems, we also attempt to disable cpus on each
    # physical processor based on "physical id" in order to keep a similar
    # number of cpus on each physical processor.
    # In addition, for each cpu we disable, we will look for cpu with same
    # "core id" in order to disable vcpu pairs.
    cpus = copy.deepcopy(self.CheckProcCpu().mappings)
    cpu_mapping = collections.defaultdict(list)
    for cpu, info in cpus.items():
      numa = info.get('physical id')
      cpu_mapping[int(numa)].append((cpu, int(info.get('core id'))))

    # Sort cpus based on 'core id' on each numa node
    for numa in cpu_mapping:
      cpu_mapping[numa] = sorted(
          cpu_mapping[numa],
          key=lambda cpu_info: (cpu_info[1], cpu_info[0]))

    def _GetNextCPUToDisable(num_disable_cpus):
      """Get the next CPU id to disable."""
      numa_nodes = list(cpu_mapping)
      while num_disable_cpus:
        for numa in sorted(numa_nodes, reverse=True):
          cpu_id, _ = cpu_mapping[numa].pop()
          num_disable_cpus -= 1
          yield cpu_id
          if not num_disable_cpus:
            break

    for cpu_id in _GetNextCPUToDisable(self.num_disable_cpus):
      self.RemoteCommand('sudo bash -c "echo 0 > '
                         f'/sys/devices/system/cpu/cpu{cpu_id}/online"')
    self._proccpu_cache = None
    self._lscpu_cache = None

  def UpdateEnvironmentPath(self):
    """Specific Linux flavors should override this."""
    pass

  def FillDisk(self):
    """Fills the primary scratch disk with a zeros file."""
    if FLAGS.disk_fill_size:
      out_file = posixpath.join(self.scratch_disks[0].mount_point, 'fill_file')
      self.RobustRemoteCommand(
          'dd if=/dev/zero of={out_file} bs=1G count={fill_size}'.format(
              out_file=out_file, fill_size=FLAGS.disk_fill_size))

  def _ApplySysctlPersistent(self, sysctl_params):
    """Apply "key=value" pairs to /etc/sysctl.conf and mark the VM for reboot.

    The reboot ensures the values take effect and remain persistent across
    future reboots.

    Args:
      sysctl_params: dict - the keys and values to write
    """
    if not sysctl_params:
      return

    for key, value in sysctl_params.items():
      self.RemoteCommand('sudo bash -c \'echo "%s=%s" >> /etc/sysctl.conf\''
                         % (key, value))

    self._needs_reboot = True

  def ApplySysctlPersistent(self, sysctl_params):
    """Apply "key=value" pairs to /etc/sysctl.conf and reboot immediately.

    The reboot ensures the values take effect and remain persistent across
    future reboots.

    Args:
      sysctl_params: dict - the keys and values to write
    """
    self._ApplySysctlPersistent(sysctl_params)
    self._RebootIfNecessary()

  def DoSysctls(self):
    """Apply --sysctl to the VM.

       The Sysctl pairs are written persistently so that if a reboot
       occurs, the flags are not lost.
    """
    sysctl_params = {}
    for pair in FLAGS.sysctl:
      key, value = pair.split('=')
      sysctl_params[key] = value
    self._ApplySysctlPersistent(sysctl_params)

  def DoConfigureNetworkForBBR(self):
    """Apply --network_enable_BBR to the VM."""
    if not FLAGS.network_enable_BBR:
      return

    if not self.kernel_release.AtLeast(4, 9):
      raise flags.ValidationError(
          'BBR requires a linux image with kernel 4.9 or newer')

    # if the current congestion control mechanism is already BBR
    # then nothing needs to be done (avoid unnecessary reboot)
    if self.TcpCongestionControl() == 'bbr':
      return

    self._ApplySysctlPersistent({
        'net.core.default_qdisc': 'fq',
        'net.ipv4.tcp_congestion_control': 'bbr'
    })

  def DoConfigureTCPWindow(self):
    """Change TCP window parameters in sysctl."""

    # Return if none of these flags are set
    if all(x is None for x in [FLAGS.tcp_max_receive_buffer,
                               FLAGS.tcp_max_send_buffer,
                               FLAGS.rmem_max,
                               FLAGS.wmem_max]):
      return

    # Get current values from VM
    stdout, _ = self.RemoteCommand('cat /proc/sys/net/ipv4/tcp_rmem')
    rmem_values = stdout.split()
    stdout, _ = self.RemoteCommand('cat /proc/sys/net/ipv4/tcp_wmem')
    wmem_values = stdout.split()
    stdout, _ = self.RemoteCommand('cat /proc/sys/net/core/rmem_max')
    rmem_max = int(stdout)
    stdout, _ = self.RemoteCommand('cat /proc/sys/net/core/wmem_max')
    wmem_max = int(stdout)

    # third number is max receive/send
    max_receive = rmem_values[2]
    max_send = wmem_values[2]

    # if flags are set, override current values from vm
    if FLAGS.tcp_max_receive_buffer:
      max_receive = FLAGS.tcp_max_receive_buffer
    if FLAGS.tcp_max_send_buffer:
      max_send = FLAGS.tcp_max_send_buffer
    if FLAGS.rmem_max:
      rmem_max = FLAGS.rmem_max
    if FLAGS.wmem_max:
      wmem_max = FLAGS.wmem_max

    # Add values to metadata
    self.os_metadata['tcp_max_receive_buffer'] = max_receive
    self.os_metadata['tcp_max_send_buffer'] = max_send
    self.os_metadata['rmem_max'] = rmem_max
    self.os_metadata['wmem_max'] = wmem_max

    rmem_string = '{} {} {}'.format(rmem_values[0],
                                    rmem_values[1],
                                    max_receive)
    wmem_string = '{} {} {}'.format(wmem_values[0],
                                    wmem_values[1],
                                    max_send)

    self._ApplySysctlPersistent({
        'net.ipv4.tcp_rmem': rmem_string,
        'net.ipv4.tcp_wmem': wmem_string,
        'net.core.rmem_max': rmem_max,
        'net.core.wmem_max': wmem_max
    })

  def _RebootIfNecessary(self):
    """Will reboot the VM if self._needs_reboot has been set."""
    if self._needs_reboot:
      self.Reboot()
      self._needs_reboot = False

  def TcpCongestionControl(self):
    """Return the congestion control used for tcp."""
    try:
      resp, _ = self.RemoteCommand(
          'cat /proc/sys/net/ipv4/tcp_congestion_control')
      return resp.rstrip('\n')
    except errors.VirtualMachine.RemoteCommandError:
      return 'unknown'

  def CheckLsCpu(self):
    """Returns a LsCpuResults from the host VM."""
    if not self._lscpu_cache:
      lscpu, _ = self.RemoteCommand('lscpu')
      self._lscpu_cache = LsCpuResults(lscpu)
    return self._lscpu_cache

  def CheckProcCpu(self):
    """Returns a ProcCpuResults from the host VM."""
    if not self._proccpu_cache:
      proccpu, _ = self.RemoteCommand('cat /proc/cpuinfo')
      self._proccpu_cache = ProcCpuResults(proccpu)
    return self._proccpu_cache

  def GetOsInfo(self) -> str:
    """Returns information regarding OS type and version."""
    stdout, _ = self.RemoteCommand('grep PRETTY_NAME /etc/os-release')
    return regex_util.ExtractGroup(OS_PRETTY_NAME_REGEXP, stdout)

  @property
  def os_info(self) -> str:
    """Get distribution-specific information."""
    if not self._os_info:
      self._os_info = self.GetOsInfo()
    return self._os_info

  @property
  def kernel_release(self) -> KernelRelease:
    """Return kernel release number."""
    if not self._kernel_release:
      self._kernel_release = KernelRelease(
          self.RemoteCommand('uname -r')[0].strip())
    return self._kernel_release

  @property
  def kernel_command_line(self) -> str:
    """Return the kernel command line."""
    if not self._kernel_command_line:
      self._kernel_command_line = self.RemoteCommand(
          'cat /proc/cmdline')[0].strip()
    return self._kernel_command_line

  @property
  def cpu_arch(self) -> str:
    """Returns the CPU architecture of the VM."""
    if not self._cpu_arch:
      self._cpu_arch = self.RemoteCommand('uname -m')[0].strip()
    return self._cpu_arch

  @property
  def partition_table(self) -> Dict[str, int]:
    """Return partition table information."""
    if not self._partition_table:
      cmd = 'sudo fdisk -l'
      partition_tables = self.RemoteCommand(cmd)[0]
      try:
        self._partition_table = {
            dev: int(size) for (dev, size) in regex_util.ExtractAllMatches(
                r'Disk\s*(.*):[\s\w\.]*,\s(\d*)\sbytes', partition_tables)}
      except regex_util.NoMatchError:
        # TODO(user): Use alternative methods to retrieve partition table.
        logging.warning('Partition table not found with "%s".', cmd)
    return self._partition_table

  @vm_util.Retry(log_errors=False, poll_interval=1)
  def WaitForBootCompletion(self):
    """Waits until the VM has booted."""
    # Test for listening on the port first, because this will happen strictly
    # first.
    if (FLAGS.cluster_boot_test_port_listening and
        self.port_listening_time is None):
      self.TestConnectRemoteAccessPort()
      self.port_listening_time = time.time()

    self._WaitForSSH()

    if self.bootable_time is None:
      self.bootable_time = time.time()

  @vm_util.Retry(log_errors=False, poll_interval=1)
  def _WaitForSSH(self):
    """Waits until the VM is ready."""
    # Always wait for remote host command to succeed, because it is necessary to
    # run benchmarks
    resp, _ = self.RemoteHostCommand('hostname', retries=1,
                                     suppress_warning=True)
    if self.hostname is None:
      self.hostname = resp[:-1]

  def RecordAdditionalMetadata(self):
    """After the VM has been prepared, store metadata about the VM."""
    self.tcp_congestion_control = self.TcpCongestionControl()
    lscpu_results = self.CheckLsCpu()
    self.numa_node_count = lscpu_results.numa_node_count
    self.os_metadata['threads_per_core'] = lscpu_results.threads_per_core
    self.os_metadata['os_info'] = self.os_info
    self.os_metadata['kernel_release'] = str(self.kernel_release)
    self.os_metadata['cpu_arch'] = self.cpu_arch
    self.os_metadata.update(self.partition_table)
    if FLAGS.append_kernel_command_line:
      self.os_metadata['kernel_command_line'] = self.kernel_command_line
      self.os_metadata[
          'append_kernel_command_line'] = FLAGS.append_kernel_command_line

    devices = self._get_network_device_mtus()
    all_mtus = set(devices.values())
    if len(all_mtus) != 1:
      raise ValueError(
          'To record, MTU must only have 1 unique MTU value not: ', devices)
    else:
      self.os_metadata['mtu'] = list(all_mtus)[0]

  # @functools.cached_property
  def _get_network_device_mtus(self) -> Dict[str, int]:
    """Returns network device names and their MTUs."""
    if not self._network_device_mtus:
      stdout, _ = self.RemoteCommand('PATH="${PATH}":/usr/sbin ip link show up')
      self._network_device_mtus = {}
      for line in stdout.splitlines():
        m = self._IP_LINK_RE_DEVICE_MTU.match(line)
        if m:
          device_name = m['device_name']
          if not any(device_name.startswith(prefix)
                     for prefix in self._IGNORE_NETWORK_DEVICE_PREFIXES):
            self._network_device_mtus[device_name] = int(m['mtu'])
    return self._network_device_mtus

  @vm_util.Retry(log_errors=False, poll_interval=1)
  def VMLastBootTime(self):
    """Returns the time the VM was last rebooted as reported by the VM.

    See
    https://unix.stackexchange.com/questions/165002/how-to-reliably-get-timestamp-at-which-the-system-booted.
    """
    stdout, _ = self.RemoteHostCommand(
        'stat -c %z /proc/', retries=1, suppress_warning=True)
    if stdout.startswith('1970-01-01'):
      # Fix for ARM returning epochtime
      date_fmt = '+%Y-%m-%d %H:%M:%S.%s %z'
      date_cmd = "grep btime /proc/stat | awk '{print $2}'"
      stdout, _ = self.RemoteHostCommand(f'date "{date_fmt}" -d@$({date_cmd})')
    return stdout

  def SnapshotPackages(self):
    """Grabs a snapshot of the currently installed packages."""
    pass

  def RestorePackages(self):
    """Restores the currently installed packages to those snapshotted."""
    pass

  def PackageCleanup(self):
    """Cleans up all installed packages.

    Deletes the temp directory, restores packages, and uninstalls all
    PerfKit packages.
    """
    for package_name in self._installed_packages:
      self.Uninstall(package_name)
    self.RestorePackages()
    self.RemoteCommand('sudo rm -rf %s' % linux_packages.INSTALL_DIR)

  def GetPathToConfig(self, package_name):
    """Returns the path to the config file for PerfKit packages.

    This function is mostly useful when config files locations
    don't match across distributions (such as mysql). Packages don't
    need to implement it if this is not the case.
    """
    pass

  def GetServiceName(self, package_name):
    """Returns the service name of a PerfKit package.

    This function is mostly useful when service names don't
    match across distributions (such as mongodb). Packages don't
    need to implement it if this is not the case.
    """
    pass

  @vm_util.Retry()
  def FormatDisk(self, device_path, disk_type=None):
    """Formats a disk attached to the VM."""
    # Some images may automount one local disk, but we don't
    # want to fail if this wasn't the case.
    if disk.NFS == disk_type:
      return
    if disk.SMB == disk_type:
      return
    umount_cmd = '[[ -d /mnt ]] && sudo umount /mnt; '
    # TODO(user): Allow custom disk formatting options.
    if FLAGS.disk_fs_type == 'xfs':
      block_size = FLAGS.disk_block_size or 512
      fmt_cmd = ('sudo mkfs.xfs -f -i size={0} {1}'.format(
          block_size, device_path))
    else:
      block_size = FLAGS.disk_block_size or 4096
      fmt_cmd = ('sudo mke2fs -F -E lazy_itable_init=0,discard -O '
                 '^has_journal -t ext4 -b {0} {1}'.format(
                     block_size, device_path))
    self.os_metadata['disk_filesystem_type'] = FLAGS.disk_fs_type
    self.os_metadata['disk_filesystem_blocksize'] = block_size
    self.RemoteHostCommand(umount_cmd + fmt_cmd)

  @vm_util.Retry(
      timeout=vm_util.DEFAULT_TIMEOUT,
      retryable_exceptions=(errors.VirtualMachine.RemoteCommandError,))
  def MountDisk(self,
                device_path,
                mount_path,
                disk_type=None,
                mount_options=disk.DEFAULT_MOUNT_OPTIONS,
                fstab_options=disk.DEFAULT_FSTAB_OPTIONS):
    """Mounts a formatted disk in the VM."""
    mount_options = '-o %s' % mount_options if mount_options else ''
    if disk.NFS == disk_type:
      mount_options = '-t nfs %s' % mount_options
      fs_type = 'nfs'
    elif disk.SMB == disk_type:
      mount_options = '-t cifs %s' % mount_options
      fs_type = 'smb'
    else:
      fs_type = FLAGS.disk_fs_type
    fstab_options = fstab_options or ''
    mnt_cmd = ('sudo mkdir -p {mount_path};'
               'sudo mount {mount_options} {device_path} {mount_path} && '
               'sudo chown $USER:$USER {mount_path};').format(
                   mount_path=mount_path,
                   device_path=device_path,
                   mount_options=mount_options)
    self.RemoteHostCommand(mnt_cmd)
    # add to /etc/fstab to mount on reboot
    mnt_cmd = ('echo "{device_path} {mount_path} {fs_type} {fstab_options}" '
               '| sudo tee -a /etc/fstab').format(
                   device_path=device_path,
                   mount_path=mount_path,
                   fs_type=fs_type,
                   fstab_options=fstab_options)
    self.RemoteHostCommand(mnt_cmd)

  def LogVmDebugInfo(self):
    """Logs the output of calling dmesg on the VM."""
    if FLAGS.log_dmesg:
      self.RemoteCommand('hostname && dmesg', should_log=True)

  def RemoteCopy(self, file_path, remote_path='', copy_to=True):
    self.RemoteHostCopy(file_path, remote_path, copy_to)

  def RemoteHostCopy(self, file_path, remote_path='', copy_to=True):
    """Copies a file to or from the VM.

    Args:
      file_path: Local path to file.
      remote_path: Optional path of where to copy file on remote host.
      copy_to: True to copy to vm, False to copy from vm.

    Raises:
      RemoteCommandError: If there was a problem copying the file.
    """
    if vm_util.RunningOnWindows():
      if ':' in file_path:
        # scp doesn't like colons in paths.
        file_path = file_path.split(':', 1)[1]
      # Replace the last instance of '\' with '/' to make scp happy.
      file_path = '/'.join(file_path.rsplit('\\', 1))
    remote_ip = '[%s]' % self.GetConnectionIp()
    remote_location = '%s@%s:%s' % (
        self.user_name, remote_ip, remote_path)
    scp_cmd = ['scp', '-P', str(self.ssh_port), '-pr']
    # An scp is not retried, so increase the connection timeout.
    ssh_private_key = (self.ssh_private_key if self.is_static else
                       vm_util.GetPrivateKeyPath())
    scp_cmd.extend(vm_util.GetSshOptions(
        ssh_private_key, connect_timeout=FLAGS.scp_connect_timeout))
    if copy_to:
      scp_cmd.extend([file_path, remote_location])
    else:
      scp_cmd.extend([remote_location, file_path])

    stdout, stderr, retcode = vm_util.IssueCommand(scp_cmd, timeout=None,
                                                   raise_on_failure=False)

    if retcode:
      full_cmd = ' '.join(scp_cmd)
      error_text = ('Got non-zero return code (%s) executing %s\n'
                    'STDOUT: %sSTDERR: %s' %
                    (retcode, full_cmd, stdout, stderr))
      raise errors.VirtualMachine.RemoteCommandError(error_text)

  def RemoteCommand(self, *args, **kwargs):
    """Runs a command on the VM.

    Args:
      *args: Arguments passed directly to RemoteCommandWithReturnCode.
      **kwargs: Keyword arguments passed directly to
          RemoteCommandWithReturnCode.

    Returns:
      A tuple of stdout, stderr from running the command.

    Raises:
      RemoteCommandError: If there was a problem establishing the connection.
    """
    return self.RemoteCommandWithReturnCode(*args, **kwargs)[:2]

  def RemoteCommandWithReturnCode(self, *args, **kwargs):
    """Runs a command on the VM.

    Args:
      *args: Arguments passed directly to RemoteHostCommandWithReturnCode.
      **kwargs: Keyword arguments passed directly to
          RemoteHostCommandWithReturnCode.

    Returns:
      A tuple of stdout, stderr, return_code from running the command.

    Raises:
      RemoteCommandError: If there was a problem establishing the connection.
    """
    return self.RemoteHostCommandWithReturnCode(*args, **kwargs)

  def RemoteHostCommandWithReturnCode(self,
                                      command,
                                      should_log=False,
                                      retries=None,
                                      ignore_failure=False,
                                      login_shell=False,
                                      suppress_warning=False,
                                      timeout=None):
    """Runs a command on the VM.

    This is guaranteed to run on the host VM, whereas RemoteCommand might run
    within i.e. a container in the host VM.

    Args:
      command: A valid bash command.
      should_log: A boolean indicating whether the command result should be
          logged at the info level. Even if it is false, the results will
          still be logged at the debug level.
      retries: The maximum number of times RemoteCommand should retry SSHing
          when it receives a 255 return code. If None, it defaults to the value
          of the flag ssh_retries.
      ignore_failure: Ignore any failure if set to true.
      login_shell: Run command in a login shell.
      suppress_warning: Suppress the result logging from IssueCommand when the
          return code is non-zero.
      timeout: The timeout for IssueCommand.

    Returns:
      A tuple of stdout, stderr, return_code from running the command.

    Raises:
      RemoteCommandError: If there was a problem establishing the connection.
    """
    if retries is None:
      retries = FLAGS.ssh_retries
    if vm_util.RunningOnWindows():
      # Multi-line commands passed to ssh won't work on Windows unless the
      # newlines are escaped.
      command = command.replace('\n', '\\n')
    ip_address = self.GetConnectionIp()
    user_host = '%s@%s' % (self.user_name, ip_address)
    ssh_cmd = ['ssh', '-A', '-p', str(self.ssh_port), user_host]
    ssh_private_key = (self.ssh_private_key if self.is_static else
                       vm_util.GetPrivateKeyPath())
    ssh_cmd.extend(vm_util.GetSshOptions(ssh_private_key))
    try:
      if login_shell:
        ssh_cmd.extend(['-t', '-t', 'bash -l -c "%s"' % command])
        self._pseudo_tty_lock.acquire()
      else:
        ssh_cmd.append(command)

      for _ in range(retries):
        stdout, stderr, retcode = vm_util.IssueCommand(
            ssh_cmd, force_info_log=should_log,
            suppress_warning=suppress_warning,
            timeout=timeout, raise_on_failure=False)
        # Retry on 255 because this indicates an SSH failure
        if retcode != RETRYABLE_SSH_RETCODE:
          break
    finally:
      if login_shell:
        self._pseudo_tty_lock.release()

    if retcode:
      full_cmd = ' '.join(ssh_cmd)
      error_text = ('Got non-zero return code (%s) executing %s\n'
                    'Full command: %s\nSTDOUT: %sSTDERR: %s' %
                    (retcode, command, full_cmd, stdout, stderr))
      if not ignore_failure:
        raise errors.VirtualMachine.RemoteCommandError(error_text)

    return (stdout, stderr, retcode)

  def RemoteHostCommand(self, *args, **kwargs):
    """Runs a command on the VM.

    This is guaranteed to run on the host VM, whereas RemoteCommand might run
    within i.e. a container in the host VM.

    Args:
      *args: Arguments passed directly to RemoteHostCommandWithReturnCode.
      **kwargs: Keyword arguments passed directly to
          RemoteHostCommandWithReturnCode.

    Returns:
      A tuple of stdout, stderr from running the command.

    Raises:
      RemoteCommandError: If there was a problem establishing the connection.
    """
    return self.RemoteHostCommandWithReturnCode(*args, **kwargs)[:2]

  def _CheckRebootability(self):
    if not self.IS_REBOOTABLE:
      raise errors.VirtualMachine.VirtualMachineError(
          "Trying to reboot a VM that isn't rebootable.")

  def _Reboot(self):
    """OS-specific implementation of reboot command."""
    self._CheckRebootability()
    self.RemoteCommand('sudo reboot', ignore_failure=True)

  def _AfterReboot(self):
    """Performs any OS-specific setup on the VM following reboot.

    This will be called after every call to Reboot().
    """
    # redetect os metadata as it might have changed
    self._os_info = None
    self._kernel_release = None
    self._kernel_command_line = None
    self._lscpu_cache = None
    self.RecordAdditionalMetadata()
    if self.install_packages:
      self._CreateInstallDir()
    self._CreateVmTmpDir()
    self._SetTransparentHugepages()
    self._has_remote_command_script = False
    self._DisableCpus()

  def MoveFile(self, target, source_path, remote_path=''):
    self.MoveHostFile(target, source_path, remote_path)

  def MoveHostFile(self, target, source_path, remote_path=''):
    """Copies a file from one VM to a target VM.

    Args:
      target: The target BaseVirtualMachine object.
      source_path: The location of the file on the REMOTE machine.
      remote_path: The destination of the file on the TARGET machine, default
          is the home directory.
    """
    self.AuthenticateVm()

    # TODO(user): For security we may want to include
    #     -o UserKnownHostsFile=/dev/null in the scp command
    #     however for the moment, this has happy side effects
    #     ie: the key is added to know known_hosts which allows
    #     OpenMPI to operate correctly.
    remote_location = '%s@%s:%s' % (
        target.user_name, target.ip_address, remote_path)
    self.RemoteHostCommand('scp -P %s -o StrictHostKeyChecking=no -i %s %s %s' %
                           (target.ssh_port, REMOTE_KEY_PATH, source_path,
                            remote_location))

  def AuthenticateVm(self):
    """Authenticate a remote machine to access all peers."""
    if not self.is_static and not self.has_private_key:
      self.RemoteHostCopy(vm_util.GetPrivateKeyPath(),
                          REMOTE_KEY_PATH)
      self.RemoteCommand(
          'echo "Host *\n  StrictHostKeyChecking no\n" > ~/.ssh/config')
      self.RemoteCommand('chmod 600 ~/.ssh/config')
      self.has_private_key = True

  def TestAuthentication(self, peer):
    """Tests whether the VM can access its peer.

    Raises:
      AuthError: If the VM cannot access its peer.
    """
    if not self.TryRemoteCommand('ssh %s hostname' % peer.internal_ip):
      raise errors.VirtualMachine.AuthError(
          'Authentication check failed. If you are running with Static VMs, '
          'please make sure that %s can ssh into %s without supplying any '
          'arguments except the ip address.' % (self, peer))

  def CheckJavaVersion(self):
    """Check the version of java on remote machine.

    Returns:
      The version of Java installed on remote machine.
    """
    version, _ = self.RemoteCommand('java -version 2>&1 >/dev/null | '
                                    'grep version | '
                                    'awk \'{print $3}\'')
    return version[:-1]

  def RemoveFile(self, filename):
    """Deletes a file on a remote machine.

    Args:
      filename: Path to the file to delete.
    """
    self.RemoteCommand('sudo rm -rf %s' % filename)

  def GetDeviceSizeFromPath(self, path):
    """Gets the size of the a drive that contains the path specified.

    Args:
      path: The function will return the amount of space on the file system
            that contains this file name.

    Returns:
      The size in 1K blocks of the file system containing the file.
    """
    df_command = "df -k -P %s | tail -n +2 | awk '{ print $2 }'" % path
    stdout, _ = self.RemoteCommand(df_command)
    return int(stdout)

  def DropCaches(self):
    """Drops the VM's caches."""
    drop_caches_command = 'sudo /sbin/sysctl vm.drop_caches=3'
    self.RemoteCommand(drop_caches_command)

  def _GetNumCpus(self):
    """Returns the number of logical CPUs on the VM.

    This method does not cache results (unlike "num_cpus").
    """
    stdout, _ = self.RemoteCommand(
        'cat /proc/cpuinfo | grep processor | wc -l')
    return int(stdout)

  def _GetTotalFreeMemoryKb(self):
    """Calculate amount of free memory in KB of the given vm.

    Free memory is calculated as sum of free, cached, and buffers
    as output from /proc/meminfo.

    Args:
      vm: vm to check

    Returns:
      free memory on the vm in KB
    """
    stdout, _ = self.RemoteCommand("""
      awk '
        BEGIN      {total =0}
        /MemFree:/ {total += $2}
        /Cached:/  {total += $2}
        /Buffers:/ {total += $2}
        END        {print total}
        ' /proc/meminfo
        """)
    return int(stdout)

  def _GetTotalMemoryKb(self):
    """Returns the amount of physical memory on the VM in Kilobytes.

    This method does not cache results (unlike "total_memory_kb").
    """
    meminfo_command = 'cat /proc/meminfo | grep MemTotal | awk \'{print $2}\''
    stdout, _ = self.RemoteCommand(meminfo_command)
    return int(stdout)

  def _TestReachable(self, ip):
    """Returns True if the VM can reach the ip address and False otherwise."""
    return self.TryRemoteCommand('ping -c 1 %s' % ip)

  def SetupLocalDisks(self):
    """Performs Linux specific setup of local disks."""
    pass

  def CreateRamDisk(self, disk_spec):
    """Performs Linux specific setup of ram disk."""
    assert disk_spec.mount_point
    ramdisk = self.RamDisk(disk_spec)
    ramdisk.Mount(self)
    self.scratch_disks.append(ramdisk)

  class RamDisk(disk.MountableDisk):
    """Linux specific setup of ram disk."""

    def Mount(self, vm):
      logging.info('Mounting and creating Ram Disk %s, %s',
                   self.mount_point, self.disk_size)
      mnt_cmd = ('sudo mkdir -p {0};sudo mount -t tmpfs -o size={1}g tmpfs {0};'
                 'sudo chown -R $USER:$USER {0};').format(
                     self.mount_point, self.disk_size)
      vm.RemoteHostCommand(mnt_cmd)

  def _CreateScratchDiskFromDisks(self, disk_spec, disks):
    """Helper method to prepare data disks.

    Given a list of BaseDisk objects, this will do most of the work creating,
    attaching, striping, formatting, and mounting them. If multiple BaseDisk
    objects are passed to this method, it will stripe them, combining them
    into one 'logical' data disk (it will be treated as a single disk from a
    benchmarks perspective). This is intended to be called from within a cloud
    specific VM's CreateScratchDisk method.

    Args:
      disk_spec: The BaseDiskSpec object corresponding to the disk.
      disks: A list of the disk(s) to be created, attached, striped,
          formatted, and mounted. If there is more than one disk in
          the list, then they will be striped together.
    """
    if len(disks) > 1:
      # If the disk_spec called for a striped disk, create one.
      disk_spec.device_path = '/dev/md%d' % len(self.scratch_disks)
      data_disk = disk.StripedDisk(disk_spec, disks)
    else:
      data_disk = disks[0]

    self.scratch_disks.append(data_disk)

    if data_disk.disk_type != disk.LOCAL:
      data_disk.Create()
      data_disk.Attach(self)

    if data_disk.is_striped:
      device_paths = [d.GetDevicePath() for d in data_disk.disks]
      self.StripeDisks(device_paths, data_disk.GetDevicePath())

    if disk_spec.mount_point:
      if isinstance(data_disk, disk.MountableDisk):
        data_disk.Mount(self)
      else:
        self.FormatDisk(data_disk.GetDevicePath(), disk_spec.disk_type)
        self.MountDisk(data_disk.GetDevicePath(), disk_spec.mount_point,
                       disk_spec.disk_type, data_disk.mount_options,
                       data_disk.fstab_options)

  def StripeDisks(self, devices, striped_device):
    """Raids disks together using mdadm.

    Args:
      devices: A list of device paths that should be striped together.
      striped_device: The path to the device that will be created.
    """
    self.Install('mdadm')
    stripe_cmd = ('yes | sudo mdadm --create %s --level=stripe --raid-devices='
                  '%s %s' % (striped_device, len(devices), ' '.join(devices)))
    self.RemoteHostCommand(stripe_cmd)

    # Save the RAID layout on the disk
    self.RemoteHostCommand('sudo mkdir -p /etc/mdadm')
    self.RemoteHostCommand('sudo touch /etc/mdadm/mdadm.conf')
    cmd = ('sudo mdadm --detail --scan | ' +
           'sudo tee -a /etc/mdadm/mdadm.conf')
    self.RemoteHostCommand(cmd)

    # Make the disk available during reboot
    init_ram_fs_cmd = self.INIT_RAM_FS_CMD
    self.RemoteHostCommand(init_ram_fs_cmd)

    # Automatically mount the disk after reboot
    cmd = ('echo \'/dev/md0  /mnt/md0  ext4 defaults,nofail'
           ',discard 0 0\' | sudo tee -a /etc/fstab')
    self.RemoteHostCommand(cmd)

  def BurnCpu(self, burn_cpu_threads=None, burn_cpu_seconds=None):
    """Burns vm cpu for some amount of time and dirty cache.

    Args:
      burn_cpu_threads: Number of threads to burn cpu.
      burn_cpu_seconds: Amount of time in seconds to burn cpu.
    """
    burn_cpu_threads = burn_cpu_threads or FLAGS.burn_cpu_threads
    burn_cpu_seconds = burn_cpu_seconds or FLAGS.burn_cpu_seconds
    if burn_cpu_seconds:
      self.InstallPackages('sysbench')
      end_time = time.time() + burn_cpu_seconds
      self.RemoteCommand(
          'nohup sysbench --num-threads=%s --test=cpu --cpu-max-prime=10000000 '
          'run 1> /dev/null 2> /dev/null &' % burn_cpu_threads)
      if time.time() < end_time:
        time.sleep(end_time - time.time())
      self.RemoteCommand('pkill -9 sysbench')

  def SetSmpAffinity(self):
    """Set SMP IRQ affinity."""
    if self._smp_affinity_script:
      self.PushDataFile(self._smp_affinity_script)
      self.RemoteCommand('sudo bash %s' % self._smp_affinity_script)
    else:
      raise NotImplementedError()

  def SetReadAhead(self, num_sectors, devices):
    """Set read-ahead value for block devices.

    Args:
      num_sectors: int. Number of sectors of read ahead.
      devices: list of strings. A list of block devices.
    """
    self.RemoteCommand(
        'sudo blockdev --setra {0} {1}; sudo blockdev --setfra {0} {1};'.format(
            num_sectors, ' '.join(devices)))

  def GetSha256sum(self, path, filename):
    """Gets the sha256sum hash for a filename in a path on the VM.

    Args:
      path: string; Path on the VM.
      filename: string; Name of the file in the path.

    Returns:
      string; The sha256sum hash.
    """
    stdout, _ = self.RemoteCommand(
        'sha256sum %s' % posixpath.join(path, filename))
    sha256sum, _ = stdout.split()
    return sha256sum

  def _GetSmbService(self):
    """Returns the SmbService created in the benchmark spec.

    Before calling this method check that the disk.disk_type is equal to
    disk.SMB or else an exception will be raised.

    Returns:
      The smb_service.BaseSmbService service for this cloud.

    Raises:
      CreationError: If no SMB service was created.
    """
    smb = getattr(context.GetThreadBenchmarkSpec(), 'smb_service')
    if smb is None:
      raise errors.Resource.CreationError('No SMB Service created')
    return smb

  def AppendKernelCommandLine(self, command_line, reboot=True):
    """Appends the provided command-line to the VM and reboots by default.

    This method should be overwritten by the desired Linux flavor to be useful.
    Most (all?) Linux flavors modify the kernel command line by updating the
    GRUB configuration files and rebooting.

    Args:
      command_line: The string to append to the kernel command line.
      reboot: Whether or not to reboot to have the change take effect.
    """
    raise NotImplementedError(
        'Kernel command-line appending for given Linux flavor not implemented.')

  def _DoAppendKernelCommandLine(self):
    """If the flag is set, attempts to append the provided kernel command line.

    In addition, to consolidate reboots during VM prepare, this method sets the
    needs reboot bit instead of immediately rebooting.
    """
    if FLAGS.disable_smt and self.CheckLsCpu().threads_per_core != 1:
      FLAGS.append_kernel_command_line = ' '.join(
          (FLAGS.append_kernel_command_line,
           'nosmt')) if FLAGS.append_kernel_command_line else 'nosmt'
    if FLAGS.append_kernel_command_line:
      self.AppendKernelCommandLine(
          FLAGS.append_kernel_command_line, reboot=False)
      self._needs_reboot = True

  @abc.abstractmethod
  def InstallPackages(self, packages: str) -> None:
    """Installs packages using the OS's package manager."""
    pass

  def _IsSmtEnabled(self):
    """Whether simultaneous multithreading (SMT) is enabled on the vm.

    Looks for the "nosmt" attribute in the booted linux kernel command line
    parameters.

    Returns:
      Whether SMT is enabled on the vm.
    """
    return not bool(re.search(r'\bnosmt\b', self.kernel_command_line))

  @property
  def cpu_vulnerabilities(self) -> CpuVulnerabilities:
    """Returns a CpuVulnerabilities of CPU vulnerabilities.

    Output of "grep . .../cpu/vulnerabilities/*" looks like this:
      /sys/devices/system/cpu/vulnerabilities/itlb_multihit:KVM: Vulnerable
      /sys/devices/system/cpu/vulnerabilities/l1tf:Mitigation: PTE Inversion
    Which gets turned into
      CpuVulnerabilities(vulnerabilities={'itlb_multihit': 'KVM'},
                         mitigations=    {'l1tf': 'PTE Inversion'})
    """
    text, _ = self.RemoteCommand(
        'sudo grep . /sys/devices/system/cpu/vulnerabilities/*',
        ignore_failure=True)
    vuln = CpuVulnerabilities()
    if not text:
      logging.warning('No text response when getting CPU vulnerabilities')
      return vuln
    for line in text.splitlines():
      vuln.AddLine(line)
    return vuln


class ClearMixin(BaseLinuxMixin):
  """Class holding Clear Linux specific VM methods and attributes."""

  OS_TYPE = os_types.CLEAR
  BASE_OS_TYPE = os_types.CLEAR
  PYTHON_2_PACKAGE = 'python-basic'

  def OnStartup(self):
    """Eliminates the need to have a tty to run sudo commands."""
    super(ClearMixin, self).OnStartup()
    self.RemoteHostCommand('sudo swupd autoupdate --disable')
    self.RemoteHostCommand('sudo mkdir -p /etc/sudoers.d')
    self.RemoteHostCommand('echo \'Defaults:{0} !requiretty\' | '
                           'sudo tee /etc/sudoers.d/pkb'.format(self.user_name),
                           login_shell=True)

  def PackageCleanup(self):
    """Cleans up all installed packages.

    Performs the normal package cleanup, then deletes the file
    added to the /etc/sudoers.d directory during startup.
    """
    super(ClearMixin, self).PackageCleanup()
    self.RemoteCommand('sudo rm /etc/sudoers.d/pkb')

  def SnapshotPackages(self):
    """See base class."""
    self.RemoteCommand('sudo swupd bundle-list > {0}/bundle_list'.format(
        linux_packages.INSTALL_DIR))

  def RestorePackages(self):
    """See base class."""
    self.RemoteCommand(
        'sudo swupd bundle-list | grep --fixed-strings --line-regexp --invert-match --file '
        '{0}/bundle_list | xargs --no-run-if-empty sudo swupd bundle-remove'
        .format(linux_packages.INSTALL_DIR),
        ignore_failure=True)

  def HasPackage(self, package):
    """Returns True iff the package is available for installation."""
    return self.TryRemoteCommand(
        'sudo swupd bundle-list --all | grep {0}'.format(package),
        suppress_warning=True)

  def InstallPackages(self, packages: str) -> None:
    """Installs packages using the swupd bundle manager."""
    self.RemoteCommand('sudo swupd bundle-add {0}'.format(packages))

  def Install(self, package_name):
    """Installs a PerfKit package on the VM."""
    if not self.install_packages:
      return
    if package_name not in self._installed_packages:
      package = linux_packages.PACKAGES[package_name]
      if hasattr(package, 'SwupdInstall'):
        package.SwupdInstall(self)
      elif hasattr(package, 'Install'):
        package.Install(self)
      else:
        raise KeyError(
            'Package {0} has no install method for Clear Linux.'.format(
                package_name))
      self._installed_packages.add(package_name)

  def Uninstall(self, package_name):
    """Uninstalls a PerfKit package on the VM."""
    package = linux_packages.PACKAGES[package_name]
    if hasattr(package, 'SwupdUninstall'):
      package.SwupdUninstall(self)
    elif hasattr(package, 'Uninstall'):
      package.Uninstall(self)

  def GetPathToConfig(self, package_name):
    """See base class."""
    package = linux_packages.PACKAGES[package_name]
    return package.SwupdGetPathToConfig(self)

  def GetServiceName(self, package_name):
    """See base class."""
    package = linux_packages.PACKAGES[package_name]
    return package.SwupdGetServiceName(self)

  def GetOsInfo(self):
    """See base class."""
    stdout, _ = self.RemoteCommand('swupd info | grep Installed')
    return 'Clear Linux build: {0}'.format(
        regex_util.ExtractGroup(CLEAR_BUILD_REGEXP, stdout))

  def SetupProxy(self):
    """Sets up proxy configuration variables for the cloud environment."""
    super(ClearMixin, self).SetupProxy()
    profile_file = '/etc/profile'
    commands = []

    if FLAGS.http_proxy:
      commands.append("echo 'export http_proxy=%s' | sudo tee -a %s" % (
          FLAGS.http_proxy, profile_file))

    if FLAGS.https_proxy:
      commands.append("echo 'https_proxy=%s' | sudo tee -a %s" % (
          FLAGS.https_proxy, profile_file))

    if FLAGS.ftp_proxy:
      commands.append("echo 'ftp_proxy=%s' | sudo tee -a %s" % (
          FLAGS.ftp_proxy, profile_file))

    if FLAGS.no_proxy:
      commands.append("echo 'export no_proxy=%s' | sudo tee -a %s" % (
          FLAGS.no_proxy, profile_file))
    if commands:
      self.RemoteCommand(';'.join(commands))

  def RemoteCommand(self, command, **kwargs):
    """Runs a command inside the container.

    Args:
      command: Arguments passed directly to RemoteHostCommandWithReturnCode.
      **kwargs: Keyword arguments passed directly to
          RemoteHostCommandWithReturnCode.

    Returns:
      A tuple of stdout and stderr from running the command.
    """
    # Escapes bash sequences
    command = '. /etc/profile; %s' % (command)
    return self.RemoteHostCommand(command, **kwargs)[:2]


class BaseContainerLinuxMixin(BaseLinuxMixin):
  """Class holding VM methods for minimal container-based OSes like Core OS.

  These operating systems have SSH like other Linux OSes, but no package manager
  to run Linux benchmarks without Docker.

  Because they cannot install packages, they only support VM life cycle
  benchmarks like cluster_boot.
  """

  def InstallPackages(self, package_name):
    raise NotImplementedError('Container OSes have no package managers.')

  def HasPackage(self, package: str) -> bool:
    return False

  # Install could theoretically be supported. A hermetic architecture
  # appropriate binary could be copied into the VM and run.
  # However because curl, wget, and object store clients cannot be installed and
  # may or may not be present, copying the binary is non-trivial so simply
  # block trying.

  def Install(self, package_name):
    raise NotImplementedError('Container OSes have no package managers.')

  def Uninstall(self, package_name):
    raise NotImplementedError('Container OSes have no package managers.')

  def PrepareVMEnvironment(self):
    # Don't try to install packages as normal, because it will fail.
    pass


class BaseRhelMixin(BaseLinuxMixin):
  """Class holding RHEL/CentOS specific VM methods and attributes."""

  # OS_TYPE = os_types.RHEL
  BASE_OS_TYPE = os_types.RHEL

  def OnStartup(self):
    """Eliminates the need to have a tty to run sudo commands."""
    super(BaseRhelMixin, self).OnStartup()
    self.RemoteHostCommand('echo \'Defaults:%s !requiretty\' | '
                           'sudo tee /etc/sudoers.d/pkb' % self.user_name,
                           login_shell=True)
    if FLAGS.gce_hpc_tools:
      self.InstallGcpHpcTools()
    if _DISABLE_YUM_CRON.value:
      # yum cron can stall causing yum commands to hang
      self.RemoteHostCommand('sudo systemctl disable yum-cron.service',
                             ignore_failure=True)

  def InstallGcpHpcTools(self):
    """Installs the GCP HPC tools."""
    self.Install('gce_hpc_tools')

  def PackageCleanup(self):
    """Cleans up all installed packages.

    Performs the normal package cleanup, then deletes the file
    added to the /etc/sudoers.d directory during startup.
    """
    super(BaseRhelMixin, self).PackageCleanup()
    self.RemoteCommand('sudo rm /etc/sudoers.d/pkb')

  def SnapshotPackages(self):
    """Grabs a snapshot of the currently installed packages."""
    self.RemoteCommand('rpm -qa > %s/rpm_package_list'
                       % linux_packages.INSTALL_DIR)

  def RestorePackages(self):
    """Restores the currently installed packages to those snapshotted."""
    self.RemoteCommand(
        'rpm -qa | grep --fixed-strings --line-regexp --invert-match --file '
        '%s/rpm_package_list | xargs --no-run-if-empty sudo rpm -e' %
        linux_packages.INSTALL_DIR,
        ignore_failure=True)

  def HasPackage(self, package):
    """Returns True iff the package is available for installation."""
    return self.TryRemoteCommand('sudo yum info %s' % package,
                                 suppress_warning=True)

  # yum talks to the network on each request so transient issues may fix
  # themselves on retry
  @vm_util.Retry(max_retries=UPDATE_RETRIES)
  def InstallPackages(self, packages):
    """Installs packages using the yum package manager."""
    self.RemoteCommand('sudo yum install -y %s' % packages)

  @vm_util.Retry()
  def InstallPackageGroup(self, package_group):
    """Installs a 'package group' using the yum package manager."""
    self.RemoteCommand('sudo yum groupinstall -y "%s"' % package_group)

  def Install(self, package_name):
    """Installs a PerfKit package on the VM."""
    if not self.install_packages:
      return
    if package_name not in self._installed_packages:
      package = linux_packages.PACKAGES[package_name]
      if hasattr(package, 'YumInstall'):
        package.YumInstall(self)
      elif hasattr(package, 'Install'):
        package.Install(self)
      else:
        raise KeyError('Package %s has no install method for RHEL.' %
                       package_name)
      self._installed_packages.add(package_name)

  def Uninstall(self, package_name):
    """Uninstalls a PerfKit package on the VM."""
    package = linux_packages.PACKAGES[package_name]
    if hasattr(package, 'YumUninstall'):
      package.YumUninstall(self)
    elif hasattr(package, 'Uninstall'):
      package.Uninstall(self)

  def GetPathToConfig(self, package_name):
    """Returns the path to the config file for PerfKit packages.

    This function is mostly useful when config files locations
    don't match across distributions (such as mysql). Packages don't
    need to implement it if this is not the case.
    """
    package = linux_packages.PACKAGES[package_name]
    return package.YumGetPathToConfig(self)

  def GetServiceName(self, package_name):
    """Returns the service name of a PerfKit package.

    This function is mostly useful when service names don't
    match across distributions (such as mongodb). Packages don't
    need to implement it if this is not the case.
    """
    package = linux_packages.PACKAGES[package_name]
    return package.YumGetServiceName(self)

  def SetupProxy(self):
    """Sets up proxy configuration variables for the cloud environment."""
    super(BaseRhelMixin, self).SetupProxy()
    yum_proxy_file = '/etc/yum.conf'

    if FLAGS.http_proxy:
      self.RemoteCommand("echo -e 'proxy= %s' | sudo tee -a %s" % (
          FLAGS.http_proxy, yum_proxy_file))

  def AppendKernelCommandLine(self, command_line, reboot=True):
    """Appends the provided command-line to the VM and reboots by default."""
    self.RemoteCommand(
        r'echo GRUB_CMDLINE_LINUX_DEFAULT=\"\${GRUB_CMDLINE_LINUX_DEFAULT} %s\"'
        ' | sudo tee -a /etc/default/grub' % command_line)
    self.RemoteCommand('sudo grub2-mkconfig -o /boot/grub2/grub.cfg')
    self.RemoteCommand('sudo grub2-mkconfig -o /etc/grub2.cfg')
    if reboot:
      self.Reboot()


class AmazonLinux2Mixin(BaseRhelMixin):
  """Class holding Amazon Linux 2 VM methods and attributes."""
  OS_TYPE = os_types.AMAZONLINUX2

  def SetupPackageManager(self):
    """Install EPEL."""
    # https://aws.amazon.com/premiumsupport/knowledge-center/ec2-enable-epel/
    self.RemoteCommand('sudo amazon-linux-extras install epel -y')


class Rhel7Mixin(BaseRhelMixin):
  """Class holding RHEL 7 specific VM methods and attributes."""
  OS_TYPE = os_types.RHEL7

  def SetupPackageManager(self):
    """Install EPEL."""
    # https://docs.fedoraproject.org/en-US/epel/#_rhel_7
    # yum exits 1 if the program is installed so check first
    self.RemoteCommand(
        f'rpm -q epel-release || sudo yum install -y {_EPEL_URL.format(7)}')


class Rhel8Mixin(BaseRhelMixin):
  """Class holding RHEL 8 specific VM methods and attributes."""
  OS_TYPE = os_types.RHEL8

  def SetupPackageManager(self):
    """Install EPEL."""
    # https://docs.fedoraproject.org/en-US/epel/#_rhel_8
    self.RemoteCommand(f'sudo dnf install -y {_EPEL_URL.format(8)}')


class Rhel9Mixin(BaseRhelMixin):
  """Class holding RHEL 9 specific VM methods and attributes."""
  OS_TYPE = os_types.RHEL9
  PYTHON_2_PACKAGE = None

  def SetupPackageManager(self):
    """Install EPEL."""
    # https://docs.fedoraproject.org/en-US/epel/#_rhel_9
    self.RemoteCommand(f'sudo dnf install -y {_EPEL_URL.format(9)}')


class CentOs7Mixin(BaseRhelMixin):
  """Class holding CentOS 7 specific VM methods and attributes."""
  OS_TYPE = os_types.CENTOS7

  def SetupPackageManager(self):
    """Install EPEL."""
    # https://docs.fedoraproject.org/en-US/epel/#_centos_7
    # yum exits 1 if the program is installed so check first
    self.RemoteCommand(
        'rpm -q epel-release || sudo yum install -y epel-release')


class CentOs8Mixin(BaseRhelMixin, virtual_machine.DeprecatedOsMixin):
  """Class holding CentOS 8 specific VM methods and attributes."""
  OS_TYPE = os_types.CENTOS8
  END_OF_LIFE = '2021-12-31'
  ALTERNATIVE_OS = f'{os_types.ROCKY_LINUX8} or {os_types.CENTOS_STREAM8}'

  def SetupPackageManager(self):
    """Install EPEL."""
    # https://docs.fedoraproject.org/en-US/epel/#almalinux_8_rocky_linux_8
    self.RemoteCommand(
        'sudo dnf config-manager --set-enabled powertools && '
        'sudo dnf install -y epel-release')


class CentOsStream8Mixin(BaseRhelMixin):
  """Class holding CentOS Stream 8 specific VM methods and attributes."""
  OS_TYPE = os_types.CENTOS_STREAM8

  def SetupPackageManager(self):
    """Install EPEL."""
    # https://docs.fedoraproject.org/en-US/epel/#_centos_stream_8
    self.RemoteCommand(
        'sudo dnf config-manager --set-enabled powertools && '
        'sudo dnf install -y epel-release epel-next-release')


class CentOsStream9Mixin(BaseRhelMixin):
  """Class holding CentOS Stream 9 specific VM methods and attributes."""
  OS_TYPE = os_types.CENTOS_STREAM9
  PYTHON_2_PACKAGE = None

  def SetupPackageManager(self):
    """Install EPEL."""
    # https://docs.fedoraproject.org/en-US/epel/#_centos_stream_9
    self.RemoteCommand(
        'sudo dnf config-manager --set-enabled crb && '
        'sudo dnf install -y epel-release epel-next-release')


class RockyLinux8Mixin(BaseRhelMixin):
  """Class holding Rocky Linux 8 specific VM methods and attributes."""
  OS_TYPE = os_types.ROCKY_LINUX8

  def SetupPackageManager(self):
    """Install EPEL."""
    # https://docs.fedoraproject.org/en-US/epel/#_almalinux_8_rocky_linux_8
    self.RemoteCommand(
        'sudo dnf config-manager --set-enabled powertools && '
        'sudo dnf install -y epel-release')


class ContainerOptimizedOsMixin(BaseContainerLinuxMixin):
  """Class holding COS specific VM methods and attributes."""
  OS_TYPE = os_types.COS
  BASE_OS_TYPE = os_types.CORE_OS

  def PrepareVMEnvironment(self):
    super(ContainerOptimizedOsMixin, self).PrepareVMEnvironment()
    # COS mounts /home and /tmp with -o noexec, which blocks running benchmark
    # binaries.
    # TODO(user): Support reboots
    self.RemoteCommand('sudo mount -o remount,exec /home')
    self.RemoteCommand('sudo mount -o remount,exec /tmp')


class CoreOsMixin(BaseContainerLinuxMixin):
  """Class holding CoreOS Container Linux specific VM methods and attributes."""
  OS_TYPE = os_types.CORE_OS
  BASE_OS_TYPE = os_types.CORE_OS


class BaseDebianMixin(BaseLinuxMixin):
  """Class holding Debian specific VM methods and attributes."""

  OS_TYPE = 'base-only'
  BASE_OS_TYPE = os_types.DEBIAN

  def __init__(self, *args, **kwargs):
    super(BaseDebianMixin, self).__init__(*args, **kwargs)

    # Whether or not apt-get update has been called.
    # We defer running apt-get update until the first request to install a
    # package.
    self._apt_updated = False

  @vm_util.Retry(max_retries=UPDATE_RETRIES)
  def AptUpdate(self):
    """Updates the package lists on VMs using apt."""
    try:
      # setting the timeout on the apt-get to 5 minutes because
      # it is known to get stuck.  In a normal update this
      # takes less than 30 seconds.
      self.RemoteCommand('sudo apt-get update', timeout=300)
    except errors.VirtualMachine.RemoteCommandError as e:
      # If there is a problem, remove the lists in order to get rid of
      # "Hash Sum mismatch" errors (the files will be restored when
      # apt-get update is run again).
      self.RemoteCommand('sudo rm -r /var/lib/apt/lists/*')
      raise e

  def SnapshotPackages(self):
    """Grabs a snapshot of the currently installed packages."""
    self.RemoteCommand(
        'dpkg --get-selections > %s/dpkg_selections'
        % linux_packages.INSTALL_DIR)

  def RestorePackages(self):
    """Restores the currently installed packages to those snapshotted."""
    self.RemoteCommand('sudo dpkg --clear-selections')
    self.RemoteCommand(
        'sudo dpkg --set-selections < %s/dpkg_selections'
        % linux_packages.INSTALL_DIR)
    self.RemoteCommand('sudo DEBIAN_FRONTEND=\'noninteractive\' '
                       'apt-get --purge -y dselect-upgrade')

  def HasPackage(self, package):
    """Returns True iff the package is available for installation."""
    if not self._apt_updated:
      self.AptUpdate()
      self._apt_updated = True
    # apt-cache show will exit 0 for purely virtual packages.
    # It does always log `N: No packages found` to STDOUT in that case though
    stdout, stderr, retcode = self.RemoteCommandWithReturnCode(
        'apt-cache --quiet=0 show ' + package,
        ignore_failure=True, should_log=True)
    return not retcode and 'No packages found' not in (stdout + stderr)

  @vm_util.Retry()
  def InstallPackages(self, packages):
    """Installs packages using the apt package manager."""
    if not self._apt_updated:
      self.AptUpdate()
      self._apt_updated = True
    try:
      install_command = ('sudo DEBIAN_FRONTEND=\'noninteractive\' '
                         '/usr/bin/apt-get -y install %s' % (packages))
      self.RemoteCommand(install_command)
    except errors.VirtualMachine.RemoteCommandError as e:
      # TODO(user): Remove code below after Azure fix their package repository,
      # or add code to recover the sources.list
      self.RemoteCommand(
          'sudo sed -i.bk "s/azure.archive.ubuntu.com/archive.ubuntu.com/g" '
          '/etc/apt/sources.list')
      logging.info('Installing "%s" failed on %s. This may be transient. '
                   'Updating package list.', packages, self)
      self.AptUpdate()
      raise e

  def Install(self, package_name):
    """Installs a PerfKit package on the VM."""
    if not self.install_packages:
      return

    if not self._apt_updated:
      self.AptUpdate()
      self._apt_updated = True

    if package_name not in self._installed_packages:
      package = linux_packages.PACKAGES[package_name]
      if hasattr(package, 'AptInstall'):
        package.AptInstall(self)
      elif hasattr(package, 'Install'):
        package.Install(self)
      else:
        raise KeyError('Package %s has no install method for Debian.' %
                       package_name)
      self._installed_packages.add(package_name)

  def Uninstall(self, package_name):
    """Uninstalls a PerfKit package on the VM."""
    package = linux_packages.PACKAGES[package_name]
    if hasattr(package, 'AptUninstall'):
      package.AptUninstall(self)
    elif hasattr(package, 'Uninstall'):
      package.Uninstall(self)
    self._installed_packages.discard(package_name)

  def GetPathToConfig(self, package_name):
    """Returns the path to the config file for PerfKit packages.

    This function is mostly useful when config files locations
    don't match across distributions (such as mysql). Packages don't
    need to implement it if this is not the case.

    Args:
      package_name: the name of the package.
    """
    package = linux_packages.PACKAGES[package_name]
    return package.AptGetPathToConfig(self)

  def GetServiceName(self, package_name):
    """Returns the service name of a PerfKit package.

    This function is mostly useful when service names don't
    match across distributions (such as mongodb). Packages don't
    need to implement it if this is not the case.

    Args:
      package_name: the name of the package.
    """
    package = linux_packages.PACKAGES[package_name]
    return package.AptGetServiceName(self)

  def SetupProxy(self):
    """Sets up proxy configuration variables for the cloud environment."""
    super(BaseDebianMixin, self).SetupProxy()
    apt_proxy_file = '/etc/apt/apt.conf'
    commands = []

    if FLAGS.http_proxy:
      commands.append("echo -e 'Acquire::http::proxy \"%s\";' |"
                      'sudo tee -a %s' % (FLAGS.http_proxy, apt_proxy_file))

    if FLAGS.https_proxy:
      commands.append("echo -e 'Acquire::https::proxy \"%s\";' |"
                      'sudo tee -a %s' % (FLAGS.https_proxy, apt_proxy_file))

    if commands:
      self.RemoteCommand(';'.join(commands))

  def IncreaseSSHConnection(self, target):
    """Increase maximum number of ssh connections on vm.

    Args:
      target: int. The max number of ssh connection.
    """
    self.RemoteCommand(r'sudo sed -i -e "s/.*MaxStartups.*/MaxStartups {0}/" '
                       '/etc/ssh/sshd_config'.format(target))
    self.RemoteCommand('sudo service ssh restart')

  def AppendKernelCommandLine(self, command_line, reboot=True):
    """Appends the provided command-line to the VM and reboots by default."""
    self.RemoteCommand(
        r'echo GRUB_CMDLINE_LINUX_DEFAULT=\"\${GRUB_CMDLINE_LINUX_DEFAULT} %s\"'
        r' | sudo tee -a /etc/default/grub' % command_line)
    self.RemoteCommand('sudo update-grub')
    if reboot:
      self.Reboot()


class Debian9Mixin(BaseDebianMixin):
  """Class holding Debian9 specific VM methods and attributes."""
  OS_TYPE = os_types.DEBIAN9
  # https://packages.debian.org/stretch/python
  PYTHON_2_PACKAGE = 'python'


class Debian10Mixin(BaseDebianMixin):
  """Class holding Debian 10 specific VM methods and attributes."""
  OS_TYPE = os_types.DEBIAN10


class Debian11Mixin(BaseDebianMixin):
  """Class holding Debian 11 specific VM methods and attributes."""
  OS_TYPE = os_types.DEBIAN11

  def PrepareVMEnvironment(self):
    # Missing in some images. Required by PrepareVMEnvironment to determine
    # partitioning.
    self.InstallPackages('fdisk')
    super().PrepareVMEnvironment()


class BaseUbuntuMixin(BaseDebianMixin):
  """Class holding Ubuntu specific VM methods and attributes."""

  def AppendKernelCommandLine(self, command_line, reboot=True):
    """Appends the provided command-line to the VM and reboots by default."""
    self.RemoteCommand(
        r'echo GRUB_CMDLINE_LINUX_DEFAULT=\"\${GRUB_CMDLINE_LINUX_DEFAULT} %s\"'
        r' | sudo tee -a /etc/default/grub.d/50-cloudimg-settings.cfg' %
        command_line)
    self.RemoteCommand('sudo update-grub')
    if reboot:
      self.Reboot()


class Ubuntu1604Mixin(BaseUbuntuMixin, virtual_machine.DeprecatedOsMixin):
  """Class holding Ubuntu1604 specific VM methods and attributes."""
  OS_TYPE = os_types.UBUNTU1604
  PYTHON_2_PACKAGE = 'python'
  END_OF_LIFE = '2021-05-01'
  ALTERNATIVE_OS = os_types.UBUNTU1804


class Ubuntu1804Mixin(BaseUbuntuMixin):
  """Class holding Ubuntu1804 specific VM methods and attributes."""
  OS_TYPE = os_types.UBUNTU1804
  # https://packages.ubuntu.com/bionic/python
  PYTHON_2_PACKAGE = 'python'

  def UpdateEnvironmentPath(self):
    """Add /snap/bin to default search path for Ubuntu1804.

    See https://bugs.launchpad.net/snappy/+bug/1659719.
    """
    self.RemoteCommand(
        r'sudo sed -i "1 i\export PATH=$PATH:/snap/bin" ~/.bashrc')
    self.RemoteCommand(
        r'sudo sed -i "1 i\export PATH=$PATH:/snap/bin" /etc/bash.bashrc')


class Ubuntu1804EfaMixin(Ubuntu1804Mixin):
  """Class holding EFA specific VM methods and attributes."""
  OS_TYPE = os_types.UBUNTU1804_EFA


# Inherit Ubuntu 18's idiosyncracies.
# Note https://bugs.launchpad.net/snappy/+bug/1659719 is also marked not fix in
# focal.
class Ubuntu2004Mixin(Ubuntu1804Mixin):
  """Class holding Ubuntu2004 specific VM methods and attributes."""
  OS_TYPE = os_types.UBUNTU2004
  # https://packages.ubuntu.com/focal/python2
  PYTHON_2_PACKAGE = 'python2'


class Ubuntu2004EfaMixin(Ubuntu2004Mixin):
  """Class holding EFA specific VM methods and attributes."""
  OS_TYPE = os_types.UBUNTU2004_EFA


class Ubuntu2204Mixin(BaseUbuntuMixin):
  """Class holding Ubuntu2204 specific VM methods and attributes."""
  OS_TYPE = os_types.UBUNTU2204


class Ubuntu1604Cuda9Mixin(Ubuntu1604Mixin):
  """Class holding NVIDIA CUDA specific VM methods and attributes."""
  OS_TYPE = os_types.UBUNTU1604_CUDA9


class ContainerizedDebianMixin(BaseDebianMixin):
  """Class representing a Containerized Virtual Machine.

  A Containerized Virtual Machine is a VM that runs remote commands
  within a Docker Container.
  Any call to RemoteCommand() will be run within the container
  whereas any call to RemoteHostCommand() will be run in the VM itself.
  """

  OS_TYPE = os_types.UBUNTU_CONTAINER
  BASE_DOCKER_IMAGE = 'ubuntu:xenial'

  def __init__(self, *args, **kwargs):
    super(ContainerizedDebianMixin, self).__init__(*args, **kwargs)
    self.docker_id = None

  def _CheckDockerExists(self):
    """Returns whether docker is installed or not."""
    resp, _ = self.RemoteHostCommand('command -v docker', ignore_failure=True,
                                     suppress_warning=True)
    if resp.rstrip() == '':
      return False
    return True

  def PrepareVMEnvironment(self):
    """Initializes docker before proceeding with preparation."""
    if not self._CheckDockerExists():
      self.Install('docker')
    # We need to explicitly create VM_TMP_DIR in the host because
    # otherwise it will be implicitly created by Docker in InitDocker()
    # (because of the -v option) and owned by root instead of perfkit,
    # causing permission problems.
    self.RemoteHostCommand('mkdir -p %s' % vm_util.VM_TMP_DIR)
    self.InitDocker()
    # This will create the VM_TMP_DIR in the container.
    # Has to be done after InitDocker() because it needs docker_id.
    self._CreateVmTmpDir()

    super(ContainerizedDebianMixin, self).PrepareVMEnvironment()

  def InitDocker(self):
    """Initializes the docker container daemon."""
    init_docker_cmd = ['sudo docker run -d '
                       '--rm '
                       '--net=host '
                       '--workdir=%s '
                       '-v %s:%s ' % (CONTAINER_WORK_DIR,
                                      vm_util.VM_TMP_DIR,
                                      CONTAINER_MOUNT_DIR)]
    for sd in self.scratch_disks:
      init_docker_cmd.append('-v %s:%s ' % (sd.mount_point, sd.mount_point))
    init_docker_cmd.append('%s sleep infinity ' % self.BASE_DOCKER_IMAGE)
    init_docker_cmd = ''.join(init_docker_cmd)

    resp, _ = self.RemoteHostCommand(init_docker_cmd)
    self.docker_id = resp.rstrip()
    return self.docker_id

  def RemoteCommand(self, command, **kwargs):
    """Runs a command inside the container.

    Args:
      command: A valid bash command.
      **kwargs: Keyword arguments passed directly to RemoteHostCommand.

    Returns:
      A tuple of stdout and stderr from running the command.
    """
    # Escapes bash sequences
    command = command.replace("'", r"'\''")

    logging.info('Docker running: %s', command)
    command = "sudo docker exec %s bash -c '%s'" % (self.docker_id, command)
    return self.RemoteHostCommand(command, **kwargs)

  def ContainerCopy(self, file_name, container_path='', copy_to=True):
    """Copies a file to or from container_path to the host's vm_util.VM_TMP_DIR.

    Args:
      file_name: Name of the file in the host's vm_util.VM_TMP_DIR.
      container_path: Optional path of where to copy file on container.
      copy_to: True to copy to container, False to copy from container.
    Raises:
      RemoteExceptionError: If the source container_path is blank.
    """
    if copy_to:
      if container_path == '':
        container_path = CONTAINER_WORK_DIR

      # Everything in vm_util.VM_TMP_DIR is directly accessible
      # both in the host and in the container
      source_path = posixpath.join(CONTAINER_MOUNT_DIR, file_name)
      command = 'cp %s %s' % (source_path, container_path)
      self.RemoteCommand(command)
    else:
      if container_path == '':
        raise errors.VirtualMachine.RemoteExceptionError('Cannot copy '
                                                         'from blank target')
      destination_path = posixpath.join(CONTAINER_MOUNT_DIR, file_name)
      command = 'cp %s %s' % (container_path, destination_path)
      self.RemoteCommand(command)

  @vm_util.Retry(
      poll_interval=1, max_retries=3,
      retryable_exceptions=(errors.VirtualMachine.RemoteCommandError,))
  def RemoteCopy(self, file_path, remote_path='', copy_to=True):
    """Copies a file to or from the container in the remote VM.

    Args:
      file_path: Local path to file.
      remote_path: Optional path of where to copy file inside the container.
      copy_to: True to copy to VM, False to copy from VM.
    """
    if copy_to:
      file_name = os.path.basename(file_path)
      tmp_path = posixpath.join(vm_util.VM_TMP_DIR, file_name)
      self.RemoteHostCopy(file_path, tmp_path, copy_to)
      self.ContainerCopy(file_name, remote_path, copy_to)
    else:
      file_name = posixpath.basename(remote_path)
      tmp_path = posixpath.join(vm_util.VM_TMP_DIR, file_name)
      self.ContainerCopy(file_name, remote_path, copy_to)
      self.RemoteHostCopy(file_path, tmp_path, copy_to)

  def MoveFile(self, target, source_path, remote_path=''):
    """Copies a file from one VM to a target VM.

    Copies a file from a container in the source VM to a container
    in the target VM.

    Args:
      target: The target ContainerizedVirtualMachine object.
      source_path: The location of the file on the REMOTE machine.
      remote_path: The destination of the file on the TARGET machine, default
          is the root directory.
    """
    file_name = posixpath.basename(source_path)

    # Copies the file to vm_util.VM_TMP_DIR in source
    self.ContainerCopy(file_name, source_path, copy_to=False)

    # Moves the file to vm_util.VM_TMP_DIR in target
    source_host_path = posixpath.join(vm_util.VM_TMP_DIR, file_name)
    target_host_dir = vm_util.VM_TMP_DIR
    self.MoveHostFile(target, source_host_path, target_host_dir)

    # Copies the file to its final destination in the container
    target.ContainerCopy(file_name, remote_path)

  def SnapshotPackages(self):
    """Grabs a snapshot of the currently installed packages."""
    pass

  def PackageCleanup(self):
    """Cleans up all installed packages.

    Stop the docker container launched with --rm.
    """
    if self.docker_id:
      self.RemoteHostCommand('docker stop %s' % self.docker_id)


def _ParseTextProperties(text):
  """Parses raw text that has lines in "key:value" form.

  When comes across an empty line will return a dict of the current values.

  Args:
    text: Text of lines in "key:value" form.

  Yields:
    Dict of [key,value] values for a section.
  """
  current_data = {}
  for line in (line.strip() for line in text.splitlines()):
    if line:
      m = _COLON_SEPARATED_RE.match(line)
      if m:
        current_data[m.group('key')] = m.group('value')
      else:
        logging.debug('Ignoring bad line "%s"', line)
    else:
      # Hit a section break
      if current_data:
        yield current_data
        current_data = {}
  if current_data:
    yield current_data


class LsCpuResults(object):
  """Holds the contents of the command lscpu."""

  def __init__(self, lscpu):
    """LsCpuResults Constructor.

    The lscpu command on Ubuntu 16.04 does *not* have the "--json" option for
    json output, so keep on using the text format.

    Args:
      lscpu: A string in the format of "lscpu" command

    Raises:
      ValueError: if the format of lscpu isnt what was expected for parsing

    Example value of lscpu is:
    Architecture:          x86_64
    CPU op-mode(s):        32-bit, 64-bit
    Byte Order:            Little Endian
    CPU(s):                12
    On-line CPU(s) list:   0-11
    Thread(s) per core:    2
    Core(s) per socket:    6
    Socket(s):             1
    NUMA node(s):          1
    Vendor ID:             GenuineIntel
    CPU family:            6
    Model:                 79
    Stepping:              1
    CPU MHz:               1202.484
    BogoMIPS:              7184.10
    Virtualization:        VT-x
    L1d cache:             32K
    L1i cache:             32K
    L2 cache:              256K
    L3 cache:              15360K
    NUMA node0 CPU(s):     0-11
    """
    self.data = {}
    for stanza in _ParseTextProperties(lscpu):
      self.data.update(stanza)

    def GetInt(key):
      if key in self.data and self.data[key].isdigit():
        return int(self.data[key])
      raise ValueError('Could not find integer "{}" in {}'.format(
          key, sorted(self.data)))

    self.numa_node_count = GetInt('NUMA node(s)')
    self.cores_per_socket = GetInt('Core(s) per socket')
    self.socket_count = GetInt('Socket(s)')
    self.threads_per_core = GetInt('Thread(s) per core')


class ProcCpuResults(object):
  """Parses /proc/cpuinfo text into grouped values.

  Most of the cpuinfo is repeated per processor.  Known ones that change per
  processor are listed in _PER_CPU_KEYS and are processed separately to make
  reporting easier.

  Example metadata for metric='proccpu':
    |bugs:spec_store_bypass spectre_v1 spectre_v2 swapgs|,
    |cache size:25344 KB|

  Example metadata for metric='proccpu_mapping':
    |proc_0:apicid=0;core id=0;initial apicid=0;physical id=0|,
    |proc_1:apicid=2;core id=1;initial apicid=2;physical id=0|

  Attributes:
    text: The /proc/cpuinfo text.
    mappings: Dict of [processor id: dict of values that change with cpu]
    attributes: Dict of /proc/cpuinfo entries that are not in mappings.
  """

  # known attributes that vary with the processor id
  _PER_CPU_KEYS = ['core id', 'initial apicid', 'apicid', 'physical id']
  # attributes that should be sorted, for example turning the 'flags' value
  # of "popcnt avx512bw" to "avx512bw popcnt"
  _SORT_VALUES = ['flags', 'bugs']

  def __init__(self, text):
    self.mappings = {}
    self.attributes = collections.defaultdict(set)
    for stanza in _ParseTextProperties(text):
      processor_id, single_values, multiple_values = self._ParseStanza(stanza)
      if processor_id is None:  # can be 0
        continue
      if processor_id in self.mappings:
        logging.warning('Processor id %s seen twice in %s', processor_id, text)
        continue
      self.mappings[processor_id] = single_values
      for key, value in multiple_values.items():
        self.attributes[key].add(value)

  def GetValues(self):
    """Dict of cpuinfo keys to its values.

    Multiple values are joined by semicolons.

    Returns:
      Dict of [cpuinfo key:value string]
    """
    cpuinfo = {
        key: ';'.join(sorted(values))
        for key, values in self.attributes.items()
    }
    cpuinfo['proccpu'] = ','.join(sorted(self.attributes.keys()))
    return cpuinfo

  def _ParseStanza(self, stanza):
    """Parses the cpuinfo section for an individual CPU.

    Args:
      stanza: Dict of the /proc/cpuinfo results for an individual CPU.

    Returns:
      Tuple of (processor_id, dict of values that are known to change with
      each CPU, dict of other cpuinfo results).
    """
    singles = {}
    if 'processor' not in stanza:
      return None, None, None
    processor_id = int(stanza.pop('processor'))
    for key in self._PER_CPU_KEYS:
      if key in stanza:
        singles[key] = stanza.pop(key)
    for key in self._SORT_VALUES:
      if key in stanza:
        stanza[key] = ' '.join(sorted(stanza[key].split()))
    return processor_id, singles, stanza


class JujuMixin(BaseDebianMixin):
  """Class to allow running Juju-deployed workloads.

  Bootstraps a Juju environment using the manual provider:
  https://jujucharms.com/docs/stable/config-manual
  """

  # TODO: Add functionality to tear down and uninstall Juju
  # (for pre-provisioned) machines + JujuUninstall for packages using charms.

  OS_TYPE = os_types.JUJU

  is_controller = False

  # A reference to the juju controller, useful when operations occur against
  # a unit's VM but need to be preformed from the controller.
  controller = None

  vm_group = None

  machines = {}
  units = []

  installation_lock = threading.Lock()

  environments_yaml = """
  default: perfkit

  environments:
      perfkit:
          type: manual
          bootstrap-host: {0}
  """

  def _Bootstrap(self):
    """Bootstrap a Juju environment."""
    resp, _ = self.RemoteHostCommand('juju bootstrap')

  def JujuAddMachine(self, unit):
    """Adds a manually-created virtual machine to Juju.

    Args:
      unit: An object representing the unit's BaseVirtualMachine.
    """
    resp, _ = self.RemoteHostCommand('juju add-machine ssh:%s' %
                                     unit.internal_ip)

    # We don't know what the machine's going to be used for yet,
    # but track it's placement for easier access later.
    # We're looking for the output: created machine %d
    machine_id = _[_.rindex(' '):].strip()
    self.machines[machine_id] = unit

  def JujuConfigureEnvironment(self):
    """Configure a bootstrapped Juju environment."""
    if self.is_controller:
      resp, _ = self.RemoteHostCommand('mkdir -p ~/.juju')

      with vm_util.NamedTemporaryFile() as tf:
        tf.write(self.environments_yaml.format(self.internal_ip))
        tf.close()
        self.PushFile(tf.name, '~/.juju/environments.yaml')

  def JujuEnvironment(self):
    """Get the name of the current environment."""
    output, _ = self.RemoteHostCommand('juju switch')
    return output.strip()

  def JujuRun(self, cmd):
    """Run a command on the virtual machine.

    Args:
      cmd: The command to run.
    """
    output, _ = self.RemoteHostCommand(cmd)
    return output.strip()

  def JujuStatus(self, pattern=''):
    """Return the status of the Juju environment.

    Args:
      pattern: Optionally match machines/services with a pattern.
    """
    output, _ = self.RemoteHostCommand('juju status %s --format=json' %
                                       pattern)
    return output.strip()

  def JujuVersion(self):
    """Return the Juju version."""
    output, _ = self.RemoteHostCommand('juju version')
    return output.strip()

  def JujuSet(self, service, params=[]):
    """Set the configuration options on a deployed service.

    Args:
      service: The name of the service.
      params: A list of key=values pairs.
    """
    output, _ = self.RemoteHostCommand(
        'juju set %s %s' % (service, ' '.join(params)))
    return output.strip()

  @vm_util.Retry(poll_interval=30, timeout=3600)
  def JujuWait(self):
    """Wait for all deployed services to be installed, configured, and idle."""
    status = yaml.safe_load(self.JujuStatus())
    for service in status['services']:
      ss = status['services'][service]['service-status']['current']

      # Accept blocked because the service may be waiting on relation
      if ss not in ['active', 'unknown']:
        raise errors.Juju.TimeoutException(
            'Service %s is not ready; status is %s' % (service, ss))

      if ss in ['error']:
        # The service has failed to deploy.
        debuglog = self.JujuRun('juju debug-log --limit 200')
        logging.warning(debuglog)
        raise errors.Juju.UnitErrorException(
            'Service %s is in an error state' % service)

      for unit in status['services'][service]['units']:
        unit_data = status['services'][service]['units'][unit]
        ag = unit_data['agent-state']
        if ag != 'started':
          raise errors.Juju.TimeoutException(
              'Service %s is not ready; agent-state is %s' % (service, ag))

        ws = unit_data['workload-status']['current']
        if ws not in ['active', 'unknown']:
          raise errors.Juju.TimeoutException(
              'Service %s is not ready; workload-state is %s' % (service, ws))

  def JujuDeploy(self, charm, vm_group):
    """Deploy (and scale) this service to the machines in its vm group.

    Args:
      charm: The charm to deploy, i.e., cs:trusty/ubuntu.
      vm_group: The name of vm_group the unit(s) should be deployed to.
    """

    # Find the already-deployed machines belonging to this vm_group
    machines = []
    for machine_id, unit in self.machines.items():
      if unit.vm_group == vm_group:
        machines.append(machine_id)

    # Deploy the first machine
    resp, _ = self.RemoteHostCommand(
        'juju deploy %s --to %s' % (charm, machines.pop()))

    # Get the name of the service
    service = charm[charm.rindex('/') + 1:]

    # Deploy to the remaining machine(s)
    for machine in machines:
      resp, _ = self.RemoteHostCommand(
          'juju add-unit %s --to %s' % (service, machine))

  def JujuRelate(self, service1, service2):
    """Create a relation between two services.

    Args:
      service1: The first service to relate.
      service2: The second service to relate.
    """
    resp, _ = self.RemoteHostCommand(
        'juju add-relation %s %s' % (service1, service2))

  def Install(self, package_name):
    """Installs a PerfKit package on the VM."""
    package = linux_packages.PACKAGES[package_name]
    try:
      # Make sure another unit doesn't try
      # to install the charm at the same time
      with self.controller.installation_lock:
        if package_name not in self.controller._installed_packages:
          package.JujuInstall(self.controller, self.vm_group)
          self.controller._installed_packages.add(package_name)
    except AttributeError as e:
      logging.warning('Failed to install package %s, falling back to Apt (%s)',
                      package_name, e)
      if package_name not in self._installed_packages:
        if hasattr(package, 'AptInstall'):
          package.AptInstall(self)
        elif hasattr(package, 'Install'):
          package.Install(self)
        else:
          raise KeyError('Package %s has no install method for Juju machines.' %
                         package_name)
        self._installed_packages.add(package_name)

  def SetupPackageManager(self):
    if self.is_controller:
      resp, _ = self.RemoteHostCommand(
          'sudo add-apt-repository ppa:juju/stable'
      )
    super(JujuMixin, self).SetupPackageManager()

  def PrepareVMEnvironment(self):
    """Install and configure a Juju environment."""
    super(JujuMixin, self).PrepareVMEnvironment()
    if self.is_controller:
      self.InstallPackages('juju')

      self.JujuConfigureEnvironment()

      self.AuthenticateVm()

      self._Bootstrap()

      # Install the Juju agent on the other VMs
      for unit in self.units:
        unit.controller = self
        self.JujuAddMachine(unit)


class BaseLinuxVirtualMachine(BaseLinuxMixin,
                              virtual_machine.BaseVirtualMachine):
  """Linux VM for use with pytyping."""
