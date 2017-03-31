# Copyright 2014
# The Cloudscaling Group, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import netaddr
from neutronclient.common import exceptions as neutron_exception
from oslo_config import cfg
from oslo_log import log as logging

from ec2api.api import clients
from ec2api.api import common
from ec2api.api import ec2utils
from ec2api.api import network_interface as network_interface_api
from ec2api.api import route_table as route_table_api
from ec2api.db import api as db_api
from ec2api import exception
from ec2api.i18n import _


CONF = cfg.CONF
LOG = logging.getLogger(__name__)


"""Subnet related API implementation
"""


Validator = common.Validator

def create_extnetwork(context, cidr_block, start, end, gatewayip):

    subnet_ipnet = netaddr.IPNetwork(cidr_block)
    ext_net_name = 'public'
    subnet_name = 'public-subnet'

    neutron = clients.neutron(context)
    with common.OnCrashCleaner() as cleaner:
        os_network_body = {
                            'network': {
                                         'name' : ext_net_name,
                                         'router:external' : True,
                                         'shared' : False,
                                       }
                          }
        try:
            os_network = neutron.create_network(os_network_body)['network']
            cleaner.addCleanup(neutron.delete_network, os_network['id'])

            os_subnet_body = {
                               'subnet': {
                                           'name': subnet_name,
                                           'network_id': os_network['id'],
                                           'ip_version': '4',
                                           'cidr': cidr_block,
                                           'allocation_pools' : [{
                                                                   'start' : start,
                                                                   'end' : end
                                                                }],
                                           'gateway_ip' : gatewayip,
                                           'enable_dhcp' : False,
                                         }
                             }

            os_subnet = neutron.create_subnet(os_subnet_body)['subnet']
            cleaner.addCleanup(neutron.delete_subnet, os_subnet['id'])
        except neutron_exception.OverQuotaClient:
            raise exception.SubnetLimitExceeded()

    return {'public-subnet': _format_ext_subnet(context, os_subnet, os_network)}

def _format_ext_subnet(context, os_subnet, os_network):
    status_map = {'ACTIVE': 'available',
                  'BUILD': 'pending',
                  'DOWN': 'available',
                  'ERROR': 'available'}
    return {
        'networkName':os_network['name'],
        'networkId': os_network['id'],
        'subnetName': os_subnet['name'],
        'subnetId': os_subnet['id'],
        'state': status_map.get(os_network['status'], 'available'),
        'cidrBlock': os_subnet['cidr'],
        'allocation-pools': os_subnet['allocation_pools'],
    }

def get_ipv6_cidr_block(context,vpc_id):
    return "2001:db8::8/64"

def create_subnet(context, vpc_id, cidr_block,ipv6_cidr_block=False,
                  availability_zone=None):
    vpc = ec2utils.get_db_item(context, vpc_id)
    vpc_ipnet = netaddr.IPNetwork(vpc['cidr_block'])
    subnet_ipnet = netaddr.IPNetwork(cidr_block)
    if subnet_ipnet not in vpc_ipnet:
        raise exception.OutOfVpcSubnetRange(cidr_block=cidr_block, vpc_ipnet=vpc_ipnet)

    if subnet_ipnet.network != subnet_ipnet.ip:
        raise exception.InvalidNetworkId(cidr_block=cidr_block, ex_cidr_block=subnet_ipnet.cidr)

    gateway_ip = str(netaddr.IPAddress(subnet_ipnet.first + 1))
    main_route_table = db_api.get_item_by_id(context, vpc['route_table_id'])

    # Check if subnet range is same as VPC range. If yes, dont add vpc route
    if vpc_ipnet.netmask == subnet_ipnet.netmask:
        host_routes = route_table_api._get_subnet_host_routes(
                context, main_route_table, gateway_ip, None, False)
    else:
        host_routes = route_table_api._get_subnet_host_routes(
                context, main_route_table, gateway_ip)

    neutron = clients.neutron(context)
    with common.OnCrashCleaner() as cleaner:
        #os_network_body = {'network': {'tenant_id':context.tenant_id}}
        os_network_body = {'network': {}}
        try:
            os_network = neutron.create_network(os_network_body)['network']
            cleaner.addCleanup(neutron.delete_network, os_network['id'])
            # NOTE(Alex): AWS takes 4 first addresses (.1 - .4) but for
            # OpenStack we decided not to support this as compatibility.
            os_subnet_body = {'subnet': {'network_id': os_network['id'],
                                         'ip_version': '4',
                                         'cidr': cidr_block,
                                         'host_routes': host_routes}}
            '''
            os_subnet_body = {'subnet': {'network_id': os_network['id'],
                                         'ip_version': '4',
                                         'cidr': cidr_block,
                                         'host_routes': host_routes,
                                         'tenant_id':context.tenant_id}}
            '''
            os_subnet = neutron.create_subnet(os_subnet_body)['subnet']
            cleaner.addCleanup(neutron.delete_subnet, os_subnet['id'])
         
            if ipv6_cidr_block == True:
                ipv6_subnet_cidr_block = get_ipv6_cidr_block(context,vpc_id)
	        os_subnet_body = {'subnet': {'network_id': os_network['id'],
                                             'ip_version': '6',
                                             'cidr': ipv6_subnet_cidr_block}}
                os_subnet_v6 = neutron.create_subnet(os_subnet_body)['subnet']
                cleaner.addCleanup(neutron.delete_subnet, os_subnet_v6['id'])
             
            else:
                os_subnet_v6={'id': ''}
        except neutron_exception.OverQuotaClient:
            raise exception.SubnetLimitExceeded()
        try:
            print 'Adding interface to router'
            neutron.add_interface_router(vpc['os_id'],
                                         {'subnet_id': os_subnet['id']})
            if ipv6_cidr_block == True:
                neutron.add_interface_router(vpc['os_id'],
                                             {'subnet_id': os_subnet_v6['id']})
        except neutron_exception.BadRequest:
            raise exception.InvalidSubnetConflict(cidr_block=cidr_block)
        cleaner.addCleanup(neutron.remove_interface_router,
                           vpc['os_id'], {'subnet_id': os_subnet['id']})
        subnet = db_api.add_item(context, 'subnet',
                                 {'os_id': os_subnet['id'],
                                  'vpc_id': vpc['id'],
                                  'os_id_v6':os_subnet_v6['id']})
        cleaner.addCleanup(db_api.delete_item, context, subnet['id'])
        neutron.update_network(os_network['id'],
                               {'network': {'name': subnet['id']}})
        neutron.update_subnet(os_subnet['id'],
                              {'subnet': {'name': subnet['id']}})
        if ipv6_cidr_block == True:
            neutron.update_subnet(os_subnet_v6['id'],
                                  {'subnet': {'name': subnet['id']+'_v6'}})
    os_ports = neutron.list_ports(tenant_id=context.project_id)['ports']
    return {'subnet': _format_subnet(context, subnet, os_subnet,
                                     os_network, os_ports, os_subnet_v6)}


def delete_subnet(context, subnet_id):
    subnet = ec2utils.get_db_item(context, subnet_id)
    if subnet.has_key('os_id_v6') and subnet['os_id_v6'] != '':
        ipv6_cidr_block = True
    vpc = db_api.get_item_by_id(context, subnet['vpc_id'])
    network_interfaces = network_interface_api.describe_network_interfaces(
        context,
        filter=[{'name': 'subnet-id',
                 'value': [subnet_id]}])['networkInterfaceSet']
    if network_interfaces:
        msg = _("The subnet '%(subnet_id)s' has dependencies and "
                "cannot be deleted.") % {'subnet_id': subnet_id}
        raise exception.DependencyViolation(msg)
    neutron = clients.neutron(context)
    with common.OnCrashCleaner() as cleaner:
        db_api.delete_item(context, subnet['id'])
        cleaner.addCleanup(db_api.restore_item, context, 'subnet', subnet)
        try:
            neutron.remove_interface_router(vpc['os_id'],
                                            {'subnet_id': subnet['os_id']})
            if ipv6_cidr_block:
                neutron.remove_interface_router(vpc['os_id'],
                                                {'subnet_id': subnet['os_id']})
        except neutron_exception.NotFound:
            pass
        cleaner.addCleanup(neutron.add_interface_router,
                           vpc['os_id'],
                           {'subnet_id': subnet['os_id']})
        if ipv6_cidr_block:
            cleaner.addCleanup(neutron.add_interface_router,
                               vpc['os_id'],
                               {'subnet_id': subnet['os_id_v6']})
        try:
            os_subnet = neutron.show_subnet(subnet['os_id'])['subnet']
            if ipv6_cidr_block:
                os_subnet = neutron.show_subnet(subnet['os_id_v6'])['subnet']
        except neutron_exception.NotFound:
            pass
        else:
            try:
                neutron.delete_network(os_subnet['network_id'])
            except neutron_exception.NetworkInUseClient as ex:
                LOG.warning(_('Failed to delete network %(os_id)s during '
                              'deleting Subnet %(id)s. Reason: %(reason)s'),
                            {'id': subnet['id'],
                             'os_id': os_subnet['network_id'],
                             'reason': ex.message})

    return True


class SubnetDescriber(common.TaggableItemsDescriber):

    KIND = 'subnet'
    FILTER_MAP = {'available-ip-address-count': 'availableIpAddressCount',
                  'cidr': 'cidrBlock',
                  'cidrBlock': 'cidrBlock',
                  'cidr-block': 'cidrBlock',
                  'subnet-id': 'subnetId',
                  'state': 'state',
                  'vpc-id': 'vpcId'}


    def format(self, subnet, os_subnet):
        if not subnet:
            return None
        os_network = next((n for n in self.os_networks
                           if n['id'] == os_subnet['network_id']),
                          None)
        if not os_network:
            self.delete_obsolete_item(subnet)
            return None
        return _format_subnet(self.context, subnet, os_subnet, os_network,
                              self.os_ports)

    def get_name(self, os_item):
        return ''

    def get_os_items(self):
        neutron = clients.neutron(self.context)
        self.os_networks = neutron.list_networks(
            tenant_id=self.context.project_id)['networks']
        self.os_ports = neutron.list_ports(
            tenant_id=self.context.project_id)['ports']
        return neutron.list_subnets()['subnets']


def describe_subnets(context, subnet_id=None, filter=None):
    formatted_subnets = SubnetDescriber().describe(context, ids=subnet_id,
                                                   filter=filter)
    return {'subnetSet': formatted_subnets}


def _format_subnet(context, subnet, os_subnet, os_network, os_ports, os_subnet_v6={}):
    status_map = {'ACTIVE': 'available',
                  'BUILD': 'pending',
                  'DOWN': 'available',
                  'ERROR': 'available'}
    cidr_range = int(os_subnet['cidr'].split('/')[1])
    # NOTE(Alex) First and last IP addresses are system ones.
    ip_count = pow(2, 32 - cidr_range) - 2
    # TODO(Alex): Probably performance-killer. Will have to optimize.
    dhcp_port_accounted = False

    # Get the vpc cidr and the route table object to trigger subnet host route cleanup
    vpc_id = subnet["vpc_id"]
    vpc = ec2utils.get_db_item(context, vpc_id)
    vpc_cidr = vpc["cidr_block"]
    vpc_cidr_range = int(vpc_cidr.split('/')[1])

    # If subnet range is same as VPC range trigger cleanup
    if cidr_range == vpc_cidr_range:
        with common.OnCrashCleaner() as cleaner:
            main_route_table = db_api.get_item_by_id(context, vpc['route_table_id'])
            route_table_api._update_subnet_host_routes(context, subnet, main_route_table, cleaner, None, None, None, True, False)
            LOG.error("Triggering host route cleanup for subnet id - {} within vpc {}".format(subnet['id'], vpc_id))

    for port in os_ports:
        for fixed_ip in port.get('fixed_ips', []):
            if fixed_ip['subnet_id'] == os_subnet['id']:
                ip_count -= 1
                if port['device_owner'] == 'network:dhcp':
                    dhcp_port_accounted = True
    if not dhcp_port_accounted:
        ip_count -= 1
    if not os_subnet_v6.has_key('cidr'):
        os_subnet_v6['cidr'] = 'False'
    return {
        'subnetId': subnet['id'],
        #'state': status_map.get(os_network['status'], 'available'),
        'vpcId': subnet['vpc_id'],
        'cidrBlock': os_subnet['cidr'],
        'cidrV6Block': os_subnet_v6['cidr'],
        #'defaultForAz': 'false',
        #'mapPublicIpOnLaunch': 'false',
        'availableIpAddressCount': ip_count
    }
