# Copyright 2014 Google Inc. All rights reserved.
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

"""Container for all data required for a benchmark to run."""

import pickle

import gflags as flags
import logging

from perfkitbenchmarker import disk
from perfkitbenchmarker import perfkitbenchmarker_lib
from perfkitbenchmarker import static_virtual_machine
from perfkitbenchmarker import virtual_machine
from perfkitbenchmarker import vm_util
from perfkitbenchmarker.aws import aws_network
from perfkitbenchmarker.aws import aws_virtual_machine
from perfkitbenchmarker.azure import azure_network
from perfkitbenchmarker.azure import azure_virtual_machine
from perfkitbenchmarker.deployment.config import config_reader
import perfkitbenchmarker.deployment.shared.ini_constants as ini_constants
from perfkitbenchmarker.gcp import gce_network
from perfkitbenchmarker.gcp import gce_virtual_machine

GCP = 'GCP'
AZURE = 'Azure'
AWS = 'AWS'
IMAGE = 'image'
MACHINE_TYPE = 'machine_type'
ZONE = 'zone'
VIRTUAL_MACHINE = 'virtual_machine'
NETWORK = 'network'
FIREWALL = 'firewall'
DEFAULTS = {
    GCP: {
        IMAGE: 'debian-7-backports',
        MACHINE_TYPE: 'n1-standard-1',
        ZONE: 'us-central1-a',
    },
    AZURE: {
        IMAGE: ('b39f27a8b8c64d52b05eac6a62ebad85__Ubuntu-'
                '14_04-LTS-amd64-server-20140724-en-us-30GB'),
        MACHINE_TYPE: 'small',
        ZONE: 'East US',
    },
    AWS: {
        IMAGE: None,
        MACHINE_TYPE: 'm3.medium',
        ZONE: 'us-east-1a'
    }
}
CLASSES = {
    GCP: {
        VIRTUAL_MACHINE: gce_virtual_machine.GceVirtualMachine,
        NETWORK: gce_network.GceNetwork,
        FIREWALL: gce_network.GceFirewall
    },
    AZURE: {
        VIRTUAL_MACHINE: azure_virtual_machine.AzureVirtualMachine,
        NETWORK: azure_network.AzureNetwork,
        FIREWALL: azure_network.AzureFirewall
    },
    AWS: {
        VIRTUAL_MACHINE: aws_virtual_machine.AwsVirtualMachine,
        NETWORK: aws_network.AwsNetwork,
        FIREWALL: aws_network.AwsFirewall
    }
}
STANDARD = 'standard'
SSD = 'ssd'
IOPS = 'iops'  # Provisioned IOPS (ssd) in AWS
DISK_TYPE = {
    GCP: {
        STANDARD: 'pd-standard',
        SSD: 'pd-ssd',
    },
    AWS: {
        STANDARD: 'standard',
        SSD: 'gp2',
        IOPS: 'io1',
    },
    AZURE: {
        STANDARD: None,  # Azure doesn't have a disk type option yet.
    }
}

FLAGS = flags.FLAGS

flags.DEFINE_enum('cloud', GCP, [GCP, AZURE, AWS], 'Name of the cloud to use.')

SSH_PORT = 22


class BenchmarkSpec(object):
  """Contains the various data required to make a benchmark run."""

  def __init__(self, benchmark_info):
    if FLAGS.benchmark_config_pair and benchmark_info[
        'name'] in FLAGS.benchmark_config_pair.keys():
      # TODO(user): Unify naming between config_reader and
      # perfkitbenchmarker.
      self.config = config_reader.ConfigLoader(
          FLAGS.benchmark_config_pair[benchmark_info['name']])
    self.vms = []
    self.vm_dict = {'default': []}
    self.networks = {}
    if hasattr(self, 'config'):
      config_dict = {}
      for section in self.config._config.sections():  # pylint: disable=protected-access
        config_dict[section] = self.config.GetSectionOptionsAsDictionary(
            section)
      self.cloud = config_dict['cluster']['type']
      self.project = config_dict['cluster']['project']
      self.zones = [config_dict['cluster']['zone']]
      self.image = []
      self.machine_type = []
      for node in self.config.node_sections:
        self.vm_dict[node.split(':')[1]] = []
      args = [((config_dict[node],
                node.split(':')[1]), {}) for node in self.config.node_sections]
      perfkitbenchmarker_lib.RunThreaded(
          self.CreateVirtualMachineFromNodeSection, args)
      self.num_vms = len(self.vms)
      self.image = ','.join(self.image)
      self.zones = ','.join(self.zones)
      self.machine_type = ','.join(self.machine_type)
    else:
      self.cloud = FLAGS.cloud
      self.project = FLAGS.project
      defaults = DEFAULTS[self.cloud]
      self.zones = FLAGS.zones or [defaults[ZONE]]
      self.image = FLAGS.image or defaults[IMAGE]
      self.machine_type = FLAGS.machine_type or defaults[
          MACHINE_TYPE]
      if benchmark_info['num_machines'] is None:
        self.num_vms = FLAGS.num_vms
      else:
        self.num_vms = benchmark_info['num_machines']
      self.scratch_disk_size = FLAGS.scratch_disk_size
      self.scratch_disk_type = FLAGS.scratch_disk_type

      self.vms = [
          self.CreateVirtualMachine(
              self.zones[min(index, len(self.zones) - 1)])
          for index in range(self.num_vms)]
      self.vm_dict['default'] = self.vms
      for i in range(benchmark_info['scratch_disk']):
        disk_spec = disk.BaseDiskSpec(
            self.scratch_disk_size,
            DISK_TYPE[self.cloud][STANDARD],
            '/scratch%d' % i)
        for vm in self.vms:
          vm.disk_specs.append(disk_spec)

    firewall_class = CLASSES[self.cloud][FIREWALL]
    self.firewall = firewall_class(self.project)
    self.file_name = '%s/%s' % (vm_util.GetTempDir(), benchmark_info['name'])
    self.deleted = False

  def Prepare(self):
    """Prepares the VMs and networks necessary for the benchmark to run."""
    if self.networks:
      prepare_args = [self.networks[zone] for zone in self.networks]
      perfkitbenchmarker_lib.RunThreaded(self.PrepareNetwork, prepare_args)
    if self.vms:
      prepare_args = [((vm, self.firewall), {}) for vm in self.vms]
      perfkitbenchmarker_lib.RunThreaded(self.PrepareVm, prepare_args)

  def Delete(self):
    if FLAGS.run_stage not in ['all', 'cleanup'] or self.deleted:
      return
    if self.vms:
      perfkitbenchmarker_lib.RunThreaded(self.DeleteVm, self.vms)
    self.firewall.DisallowAllPorts()
    for zone in self.networks:
      self.networks[zone].Delete()
    self.deleted = True

  def PrepareNetwork(self, network):
    """Initialize the network."""
    network.Create()

  def CreateVirtualMachine(self, opt_zone=None):
    """Create a vm in zone.

    Args:
      opt_zone: The zone in which the vm will be created. If not provided,
        FLAGS.zone or the revelant zone from DEFAULT will be used.
    Returns:
      A vm object.
    """
    vm = static_virtual_machine.StaticVirtualMachine.GetStaticVirtualMachine()
    if vm:
      return vm
    vm_class = CLASSES[self.cloud][VIRTUAL_MACHINE]
    zone = opt_zone or self.zones[0]
    if zone not in self.networks:
      network_class = CLASSES[self.cloud][NETWORK]
      self.networks[zone] = network_class(zone)
    self.vm_spec = virtual_machine.BaseVirtualMachineSpec(
        self.project, zone, self.machine_type, self.image,
        self.networks[zone])
    return vm_class(self.vm_spec)

  def CreateVirtualMachineFromNodeSection(self, node_section, node_name):
    """Create a VirtualMachine object from NodeSection.

    Args:
      node_section: A dictionary of (option name, option value) pairs.
      node_name: The name of node.
    """
    zone = node_section['zone'] if 'zone' in node_section else self.zones[0]
    if zone not in self.zones:
      self.zones.append(zone)
    if node_section['image'] not in self.image:
      self.image.append(node_section['image'])
    if node_section['vm_type'] not in self.machine_type:
      self.machine_type.append(node_section['vm_type'])
    if zone not in self.networks:
      network_class = CLASSES[self.cloud][NETWORK]
      self.networks[zone] = network_class(zone)
    vm_spec = virtual_machine.BaseVirtualMachineSpec(
        self.project,
        zone,
        node_section['vm_type'],
        node_section['image'],
        self.networks[zone])
    vm_class = CLASSES[self.cloud][VIRTUAL_MACHINE]
    vms = [vm_class(vm_spec) for _ in range(int(node_section['count']))]
    self.vms.extend(vms)
    self.vm_dict[node_name].extend(vms)
    # Create disk spec.
    for option in node_section:
      if option.startswith(ini_constants.OPTION_PD_PREFIX):
        # Create disk spec.
        disk_size, disk_type, mnt_point = node_section[option].split(':')
        disk_size = int(disk_size)
        disk_spec = disk.BaseDiskSpec(
            disk_size, DISK_TYPE[self.cloud][disk_type], mnt_point)
        for vm in vms:
          vm.disk_specs.append(disk_spec)

  def PrepareVm(self, vm, firewall):
    """Creates a single VM and prepares a scratch disk if required.

    Args:
        vm: The BaseVirtualMachine object representing the VM.
        firewall: The BaseFirewall object representing the firewall.
    """
    vm.Create()
    logging.info('VM: %s', vm.ip_address)
    logging.info('Waiting for boot completion.')
    firewall.AllowPort(vm, SSH_PORT)
    vm.WaitForBootCompletion()
    vm.AptUpdate()
    for disk_spec in vm.disk_specs:
      vm.CreateScratchDisk(disk_spec)
    perfkitbenchmarker_lib.BurnCpu(vm)

  def DeleteVm(self, vm):
    """Deletes a single vm and scratch disk if required.

    Args:
        vm: The BaseVirtualMachine object representing the VM.
    """
    vm.Delete()
    vm.DeleteScratchDisks()

  def PickleSpec(self):
    """Pickles the spec so that it can be unpickled on a subsequent run."""
    with open(self.file_name, 'wb') as pickle_file:
      pickle.dump(self, pickle_file, 2)

  @classmethod
  def GetSpecFromFile(cls, name):
    """Unpickles the spec and returns it.

    Args:
      name: The name of the benchmark (and the name of the pickled file).

    Returns:
      A BenchmarkSpec object.
    """
    file_name = '%s/%s' % (vm_util.GetTempDir(), name)
    try:
      with open(file_name, 'rb') as pickle_file:
        spec = pickle.load(pickle_file)
    except Exception as e:  # pylint: disable=broad-except
      logging.error('Unable to unpickle spec file for benchmark %s.', name)
      raise e
    return spec
