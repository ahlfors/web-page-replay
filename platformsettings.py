#!/usr/bin/env python
# Copyright 2010 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import fileinput
import logging
import os
import platform
import re
import subprocess
import tempfile


class PlatformSettingsError(Exception):
  """Module catch-all error."""
  pass


class DnsReadError(PlatformSettingsError):
  """Raised when unable to read DNS settings."""
  pass


class DnsUpdateError(PlatformSettingsError):
  """Raised when unable to update DNS settings."""
  pass


class PlatformSettings(object):
  _IPFW_BIN = None

  def __init__(self):
    self.original_primary_dns = None

  def get_primary_dns(self):
    raise NotImplementedError()

  def get_original_primary_dns(self):
    if not self.original_primary_dns:
      self.original_primary_dns = self.get_primary_dns()
    return self.original_primary_dns

  def set_primary_dns(self, dns):
    if not self.original_primary_dns:
      self.original_primary_dns = self.get_primary_dns()
    self._set_primary_dns(dns)
    if self.get_primary_dns() == dns:
      logging.info('Changed system DNS to %s', dns)
    else:
      raise self._get_dns_update_error()

  def restore_primary_dns(self):
    if not self.original_primary_dns:
      raise DnsUpdateError('Cannot restore because never set.')
    self.set_primary_dns(self.original_primary_dns)
    self.original_primary_dns = None

  def ipfw(self, *args):
    if self._IPFW_BIN:
      ipfw_args = [self._IPFW_BIN] + [str(a) for a in args]
      logging.debug(' '.join(ipfw_args))
      subprocess.check_call(ipfw_args)
    else:
      raise NotImplementedError()

  def is_cwnd_available(self):
    return False

  def set_cwnd(self, args):
    logging.error("Platform does not support setting cwnd.")

  def get_cwnd(self):
    logging.error("Platform does not support getting cwnd.")

  def get_ipfw_queue_slots(self):
    return 500

  def configure_loopback(self):
    """
    Configure loopback to be realistic.

    We use loopback for much of our testing, and on some systems, loopback
    behaves differently from real interfaces.
    """
    logging.error("Platform does not support loopback configuration.")

  def unconfigure_loopback(self):
    pass


class PosixPlatformSettings(PlatformSettings):
  _IPFW_BIN = 'ipfw'

  def _get_dns_update_error(self):
    return DnsUpdateError('Did you run under sudo?')

  def _sysctl(self, *args):
    sysctl = subprocess.Popen(
        ['sysctl'] + [str(a) for a in args],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    stdout = sysctl.communicate()[0]
    return sysctl.returncode, stdout

  def has_sysctl(self, name):
    return self._sysctl(name)[0] == 0

  def set_sysctl(self, name, value):
    rv = self._sysctl('%s=%s' % (name, value))[0]
    if rv != 0:
      logging.error("Unable to set sysctl %s: %s", name, rv)

  def get_sysctl(self, name):
    rv, value = self._sysctl('-n', name)
    if rv == 0:
      return value
    else:
      logging.error("Unable to get sysctl %s: %s", name, rv)
      return None


class OsxPlatformSettings(PosixPlatformSettings):
  LOCAL_SLOWSTART_MIB_NAME = 'net.inet.tcp.local_slowstart_flightsize'

  def _scutil(self, cmd):
    scutil = subprocess.Popen(
        ['scutil'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    return scutil.communicate(cmd)[0]

  def _ifconfig(self, *args):
    ifconfig = subprocess.Popen(
        ['ifconfig'] + [str(a) for a in args],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    return ifconfig.communicate()[0]

  def set_sysctl(self, name, value):
    rv = self._sysctl('-w', '%s=%s' % (name, value))[0]
    if rv != 0:
      logging.error("Unable to set sysctl %s: %s", name, rv)

  def _get_dns_service_key(self):
    # <dictionary> {
    #   PrimaryInterface : en1
    #   PrimaryService : 8824452C-FED4-4C09-9256-40FB146739E0
    #   Router : 192.168.1.1
    # }
    output = self._scutil('show State:/Network/Global/IPv4')
    lines = output.split('\n')
    for line in lines:
      key_value = line.split(' : ')
      if key_value[0] == '  PrimaryService':
        return 'State:/Network/Service/%s/DNS' % key_value[1]
    raise self._get_dns_update_error()

  def get_primary_dns(self):
    # <dictionary> {
    #   ServerAddresses : <array> {
    #     0 : 198.35.23.2
    #     1 : 198.32.56.32
    #   }
    #   DomainName : apple.co.uk
    # }
    output = self._scutil('show %s' % self._get_dns_service_key())
    primary_line = output.split('\n')[2]
    line_parts = primary_line.split(' ')
    return line_parts[-1]

  def _set_primary_dns(self, dns):
    command = '\n'.join([
      'd.init',
      'd.add ServerAddresses * %s' % dns,
      'set %s' % self._get_dns_service_key()
    ])
    self._scutil(command)

  def get_ipfw_queue_slots(self):
    return 100

  def get_loopback_mtu(self):
    config = self._ifconfig('lo0')
    match = re.search(r'\smtu\s+(\d+)', config)
    if match:
      return int(match.group(1))
    else:
      return None

  def is_cwnd_available(self):
    return True

  def set_cwnd(self, size):
    self.set_sysctl(self.LOCAL_SLOWSTART_MIB_NAME, size)

  def get_cwnd(self):
    return int(self.get_sysctl(self.LOCAL_SLOWSTART_MIB_NAME))

  def configure_loopback(self):
    """Configure loopback to use reasonably sized frames.

    OS X uses jumbo frames by default (16KB).
    """
    TARGET_LOOPBACK_MTU = 1500
    loopback_mtu = self.get_loopback_mtu()
    if loopback_mtu and loopback_mtu != TARGET_LOOPBACK_MTU:
      self.saved_loopback_mtu = loopback_mtu
      self._ifconfig('lo0', 'mtu', TARGET_LOOPBACK_MTU)
      logging.debug('Set loopback MTU to %d (was %d)',
                    TARGET_LOOPBACK_MTU, loopback_mtu)
    else:
      logging.error('Unable to read loopback mtu. Setting left unchanged.')

  def unconfigure_loopback(self):
    if hasattr(self, 'saved_loopback_mtu') and self.saved_loopback_mtu:
      self._ifconfig('lo0', 'mtu', self.saved_loopback_mtu)
      logging.debug('Restore loopback MTU to %d', self.saved_loopback_mtu)


class LinuxPlatformSettings(PosixPlatformSettings):
  """The following thread recommends a way to update DNS on Linux:

  http://ubuntuforums.org/showthread.php?t=337553

         sudo cp /etc/dhcp3/dhclient.conf /etc/dhcp3/dhclient.conf.bak
         sudo gedit /etc/dhcp3/dhclient.conf
         #prepend domain-name-servers 127.0.0.1;
         prepend domain-name-servers 208.67.222.222, 208.67.220.220;

         prepend domain-name-servers 208.67.222.222, 208.67.220.220;
         request subnet-mask, broadcast-address, time-offset, routers,
             domain-name, domain-name-servers, host-name,
             netbios-name-servers, netbios-scope;
         #require subnet-mask, domain-name-servers;

         sudo/etc/init.d/networking restart

  The code below does not try to change dchp and does not restart networking.
  Update this as needed to make it more robust on more systems.
  """
  RESOLV_CONF = '/etc/resolv.conf'
  TCP_INIT_CWND = 'net.ipv4.tcp_init_cwnd'
  TCP_BASE_MSS = 'net.ipv4.tcp_base_mss'
  TCP_MTU_PROBING = 'net.ipv4.tcp_mtu_probing'

  def get_primary_dns(self):
    try:
      resolv_file = open(self.RESOLV_CONF)
    except IOError:
      raise DnsReadError()
    for line in resolv_file:
      if line.startswith('nameserver '):
        return line.split()[1]
    raise DnsReadError()

  def _set_primary_dns(self, dns):
    """Replace the first nameserver entry with the one given."""
    self._write_resolve_conf(dns)

  def _write_resolve_conf(self, dns):
    is_first_nameserver_replaced = False
    # The fileinput module uses sys.stdout as the edited file output.
    for line in fileinput.input(self.RESOLV_CONF, inplace=1, backup='.bak'):
      if line.startswith('nameserver ') and not is_first_nameserver_replaced:
        print 'nameserver %s' % dns
        is_first_nameserver_replaced = True
      else:
        print line,
    if not is_first_nameserver_replaced:
      raise DnsUpdateError('Could not find a suitable namserver entry in %s' %
                           self.RESOLV_CONF)

  def is_cwnd_available(self):
    return self.has_sysctl(self.TCP_INIT_CWND)

  def set_cwnd(self, args):
    self.set_sysctl(self.TCP_INIT_CWND, str(args))

  def get_cwnd(self):
    return self.get_sysctl(self.TCP_INIT_CWND)

  def configure_loopback(self):
    """
    Linux will use jumbo frames by default (16KB), using the combination
    of MTU probing and a base MSS makes it use normal sized packets.

    The reason this works is because tcp_base_mss is only used when MTU
    probing is enabled.  And since we're using the max value, it will
    always use the reasonable size.  This is relevant for server-side realism.
    The client-side will vary depending on the client TCP stack config.
    """
    ENABLE_MTU_PROBING = 2
    TCP_FULL_MSS = 1460
    self.saved_tcp_mtu_probing = self.get_sysctl(self.TCP_MTU_PROBING)
    self.set_sysctl(self.TCP_MTU_PROBING, ENABLE_MTU_PROBING)
    self.saved_tcp_base_mss = self.get_sysctl(self.TCP_BASE_MSS)
    self.set_sysctl(self.TCP_BASE_MSS, TCP_FULL_MSS)

  def unconfigure_loopback(self):
    if self.saved_tcp_mtu_probing:
      self.set_sysctl(self.TCP_MTU_PROBING, self.saved_tcp_mtu_probing)
    if self.saved_tcp_base_mss:
      self.set_sysctl(self.TCP_BASE_MSS, self.saved_tcp_base_mss)

class WindowsPlatformSettings(PlatformSettings):
  def _get_dns_update_error(self):
    return DnsUpdateError('Did you run as administrator?')

  def _netsh_show_dns(self):
    """Return DNS information:

    Example output:

    Configuration for interface "Local Area Connection 3"
    DNS servers configured through DHCP:  None
    Register with which suffix:           Primary only

    Configuration for interface "Wireless Network Connection 2"
    DNS servers configured through DHCP:  192.168.1.1
    Register with which suffix:           Primary only
    """
    return subprocess.Popen(
        ['netsh', 'interface', 'ip', 'show', 'dns'],
        stdout=subprocess.PIPE).communicate()[0]

  def _netsh_get_interface_names(self):
    return re.findall(r'"(.+?)"', self._netsh_show_dns())

  def get_primary_dns(self):
    match = re.search(r':\s+(\d+\.\d+\.\d+\.\d+)', self._netsh_show_dns())
    return match and match.group(1) or None

  def _set_primary_dns(self, dns):
    vbs = """Set objWMIService = GetObject("winmgmts:{impersonationLevel=impersonate}!\\\\.\\root\\cimv2")
Set colNetCards = objWMIService.ExecQuery("Select * From Win32_NetworkAdapterConfiguration Where IPEnabled = True")
For Each objNetCard in colNetCards
  arrDNSServers = Array("%s")
  objNetCard.SetDNSServerSearchOrder(arrDNSServers)
Next
""" % dns
    vbs_file = tempfile.NamedTemporaryFile(suffix='.vbs', delete=False)
    vbs_file.write(vbs)
    vbs_file.close()
    subprocess.check_call(['cscript', '//nologo', vbs_file.name])
    os.remove(vbs_file.name)


class WindowsXpPlatformSettings(WindowsPlatformSettings):
  _IPFW_BIN = r'third_party\ipfw_win32\ipfw.exe'


def _new_platform_settings():
  """Make a new instance of PlatformSettings for the current system."""
  system = platform.system()
  release = platform.release()
  if system == 'Darwin':
    return OsxPlatformSettings()
  if system == 'Linux':
    return LinuxPlatformSettings()
  if system == 'Windows':
    if release == 'XP':
      return WindowsXpPlatformSettings()
    else:
      return WindowsPlatformSettings()
  raise NotImplementedError('Sorry %s %s is not supported.' % (system, release))

_platform_settings = None
def get_platform_settings():
  """Return a single instance of PlatformSettings."""
  global _platform_settings
  if not _platform_settings:
    _platform_settings = _new_platform_settings()
  return _platform_settings
