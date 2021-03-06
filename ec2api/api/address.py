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

try:
    from neutronclient.common import exceptions as neutron_exception
except ImportError:
    pass  # clients will log absense of neutronclient in this case
from novaclient import exceptions as nova_exception
from oslo_config import cfg
from oslo_log import log as logging

from ec2api.api import clients
from ec2api.api import common
from ec2api.api import ec2utils
from ec2api.api import internet_gateway as internet_gateway_api
from ec2api.db import api as db_api
from ec2api import exception
from ec2api.i18n import _
import paramiko
import re   

CONF = cfg.CONF

address_opts = [
    cfg.StrOpt('router_address',
               default='',
               help='Address of router to get routes'),
    cfg.StrOpt('router_user',
               default='',
               help='Username for router'),
    cfg.StrOpt('router_cred',
               default='',
               help='Creds for Router'),
]

CONF = cfg.CONF
CONF.register_opts(address_opts)
LOG = logging.getLogger(__name__)

RouterIP = CONF.router_address
RouterUser = CONF.router_user
RouterCred = CONF.router_cred 

Status = ['active', 'pending']

"""Address related API implementation
"""


Validator = common.Validator



def get_rt_ip_status(publicIp):
    status = Status[1]
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(RouterIP, username=RouterUser, password=RouterCred)
    stdin, stdout, stderr = client.exec_command('show route '+publicIp +' detail'+ ' | grep \"Protocol next hop\"')
    for line in stdout:
        if re.match('\s+Protocol next hop: \d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}.*', line) !=None :
            status = Status[0]
            break
    client.close()
    return status


    ### This function is called in order to remove any descrepancies
    ### between ec2-api db and cassandra db as terminateInstances will
    ### remove eni entry but the same will not be removed from floating
    ### ip from ec2 db.

def validate_db(context,address):

    if address.get('network_interface_id') :
        neutron = clients.neutron(context)
        os_address =  neutron.show_floatingip(address['os_id'])['floatingip']
        if (not os_address.get('port_id') or
             os_address['fixed_ip_address'] != address['private_ip_address']):
            _disassociate_address_item(context, address)
            LOG.error("Validate db failed - Auto update triggered disassociation - Local DB item : {} OS item : {}".format(str(address), str(os_address)))



def get_address_engine():
    if CONF.full_vpc_support:
        return AddressEngineNeutron()
    else:
        return AddressEngineNova()


def allocate_address(context, domain=None):
    #Commenting below code as we are not supporting standard domain right now.
    #if domain and domain not in ['vpc', 'standard']:
    #    msg = _("Invalid value '%(domain)s' for domain.") % {'domain': domain}
    #    raise exception.InvalidParameterValue(msg)

    if domain and domain not in ['vpc']:
        msg = _("Invalid value '%(domain)s' for domain.") % {'domain': domain}
        raise exception.InvalidParameterValue(msg)

    address, os_floating_ip = address_engine.allocate_address(context, domain)
    return _format_address(context, address, os_floating_ip)


def associate_address(context, public_ip=None, instance_id=None,
                      allocation_id=None, network_interface_id=None,
                      private_ip_address=None):
    #                  private_ip_address=None, allow_reassociation=False):
    if not public_ip and not allocation_id:
        msg = _('Either public IP or allocation id must be specified')
        raise exception.MissingParameter(msg)
    if public_ip and allocation_id:
        msg = _('You may specify public IP or allocation id, '
                'but not both in the same call')
        raise exception.InvalidParameterCombination(msg)
    if not instance_id and not network_interface_id:
        msg = _('Either instance ID or network interface id must be specified')
        raise exception.MissingParameter(msg)
    associationId = address_engine.associate_address(
        context, public_ip, instance_id,
        allocation_id, network_interface_id,
        private_ip_address)
    #    private_ip_address, allow_reassociation)
    if associationId:
        return {'return': True,
                'associationId': associationId,
                'status': Status[1]}
    return {'return': True}


def disassociate_address(context, public_ip=None, association_id=None):
    if not public_ip and not association_id:
        msg = _('Either public IP or association id must be specified')
        raise exception.MissingParameter(msg)
    if public_ip and association_id:
        msg = _('You may specify public IP or association id, '
                'but not both in the same call')
        raise exception.InvalidParameterCombination(msg)
    status = address_engine.disassociate_address(context, public_ip, association_id)
    return {'return': True, 'status': status}


def release_address(context, public_ip=None, allocation_id=None):
    if not public_ip and not allocation_id:
        msg = _('Either public IP or allocation id must be specified')
        raise exception.MissingParameter(msg)
    if public_ip and allocation_id:
        msg = _('You may specify public IP or allocation id, '
                'but not both in the same call')
        raise exception.InvalidParameterCombination(msg)

    address_engine.release_address(context, public_ip, allocation_id)
    return True


class AddressDescriber(common.UniversalDescriber):

    KIND = 'eipalloc'
    FILTER_MAP = {'allocation-id': 'allocationId',
                  'association-id': 'associationId',
                  'domain': 'domain',
                  'instance-id': 'instanceId',
                  'network-interface-id': 'networkInterfaceId',
                  'network-interface-owner-id': 'networkInterfaceOwnerId',
                  'private-ip-address': 'privateIpAddress',
                  'public-ip': 'publicIp',
                  'status': 'status'}

    def __init__(self, os_ports, db_instances):
        self.os_ports = os_ports
        self.db_instances_dict = {i['os_id']: i for i in (db_instances or [])}

    def format(self, item=None, os_item=None):
        return _format_address(self.context, item, os_item, self.os_ports,
                               self.db_instances_dict)

    def get_os_items(self):
        return address_engine.get_os_floating_ips(self.context)

    def auto_update_db(self, item, os_item):
        item = super(AddressDescriber, self).auto_update_db(item, os_item)
        if (item and 'network_interface_id' in item and
                (not os_item.get('port_id') or
                 os_item['fixed_ip_address'] != item['private_ip_address'])):
            _disassociate_address_item(self.context, item)
            LOG.error("Auto update triggered disassociation - Local DB item : {} OS item : {}".format(str(item), str(os_item)))
        
        
        ## still need to change active and inactive
        # If it has network_interface and status is inactive then association is happened
        # check whether new network is available in router 
        # If it doesn't have network interface and status is active
        # then disassociation has happened check for update
        if (item and 'status' in item) :
            if (item['status'] == Status[1] ) :
                
                item['status'] = get_rt_ip_status(item['public_ip'])
                
                if('network_interface_id' in item) :                
                    #check for route
                    LOG.error('Address {} is pending and is associated. Adding status as {}'.format(str(item), item['status']))
                    if item['status'] == Status[0] :
                        _update_status(self.context, item, Status[0])

                else :
                    LOG.error('Address {} is pending and not associated. Popping status field'.format(str(item)))
                    _pop_status(self.context, item)
                    
            elif ( item['status'] == Status[0] and 'network_interface_id' not in item ) :
                #check for route
                item['status'] = get_rt_ip_status(item['public_ip']) 
                LOG.error('Address {} is disassociated and active. Current status is {}'.format(str(item), item['status']))
                #pop if status is inactive
                if item['status'] ==Status[1] :
                    _pop_status(self.context, item)
        
        #This is for migration whenever an old associated address is described.    
        if (item and 'network_interface_id' in item and 'status' not in item) :
            #check for routes
            item['status'] = get_rt_ip_status(item['public_ip'])
            LOG.error('Address {} do not have status. Adding status as {}'.format(str(item), item['status']))
            _update_status(self.context, item, item['status'])
            
            
        return item

    def get_name(self, os_item):
        return os_item['floating_ip_address']


def describe_addresses(context, public_ip=None, allocation_id=None,
                       filter=None):
    formatted_addresses = AddressDescriber(
        address_engine.get_os_ports(context),
        db_api.get_items(context, 'i')).describe(
            context, allocation_id, public_ip, filter)
    return {'addressesSet': formatted_addresses}

## Modify this function by adding bgp routes here.
## Also take care for vagrant environment.
## Make sure you read from your env.

def _format_address(context, address, os_floating_ip, os_ports=[],
                    db_instances_dict=None):
    ec2_address = {'publicIp': os_floating_ip['floating_ip_address']}
    fixed_ip_address = os_floating_ip.get('fixed_ip_address')
    if fixed_ip_address:
        ec2_address['privateIpAddress'] = fixed_ip_address
        port_id = os_floating_ip.get('port_id')
        os_fip = os_floating_ip.get('instance_id')
        if port_id:
            port = next((port for port in os_ports
                         if port['id'] == port_id), None)
            if port and port.get('device_id'):
                ec2_address['instanceId'] = (
                    _get_instance_ec2_id_by_os_id(context, port['device_id'],
                                                  db_instances_dict))
        elif os_fip:
            ec2_address['instanceId'] = (
                _get_instance_ec2_id_by_os_id(context, os_fip,
                                              db_instances_dict))
    if not address:
        ec2_address['domain'] = 'standard'
    else:
        ec2_address['domain'] = 'vpc'
        ec2_address['allocationId'] = address['id']
        if 'network_interface_id' in address:
            ec2_address.update({
                    'associationId': ec2utils.change_ec2_id_kind(
                            ec2_address['allocationId'], 'eipassoc'),
                    'networkInterfaceId': address['network_interface_id'],
                    'networkInterfaceOwnerId': context.project_id,
                    'status': address['status'] })

        #Show associationId if status is in the db adn network_interface is not
        elif 'status' in address:
            ec2_address.update({
                    'associationId': ec2utils.change_ec2_id_kind(
                            ec2_address['allocationId'], 'eipassoc'),
                    'status': address['status'] })  
            
    return ec2_address


def _get_instance_ec2_id_by_os_id(context, os_instance_id, db_instances_dict):
    db_item = ec2utils.get_db_item_by_os_id(context, 'i', os_instance_id,
                                            db_instances_dict)
    return db_item['id']


def _is_address_valid(context, neutron, address):
    try:
        neutron.show_floatingip(address['os_id'])
    except neutron_exception.NotFound:
        return False
    else:
        return True


def _associate_address_item(context, address, network_interface_id,
                            private_ip_address):
    LOG.debug("Associating address in DB : {} nid: {} pip: {}".format(str(address),str(network_interface_id),str(private_ip_address)))
    address['network_interface_id'] = network_interface_id
    address['private_ip_address'] = private_ip_address            
    address['status'] = Status[1]
    db_api.update_item(context, address)
    

def _disassociate_address_item(context, address):
    LOG.debug("Disassociating address in DB : {}".format(str(address)))
    address.pop('network_interface_id')
    address.pop('private_ip_address')
    
    # This will help in migration of already associated IP
    # where there are ip's that are allocated but do not have status
    address['status'] = get_rt_ip_status(address['public_ip'])
        
    db_api.update_item(context, address)
    return address['status']

def _update_status(context,address, status):
    address['status'] = status
    db_api.update_item(context,address)
    
def _pop_status(context,address):
    address.pop('status')
    db_api.update_item(context,address)


class AddressEngineNeutron(object):

    def allocate_address(self, context, domain=None):
        if not domain or domain == 'standard':
            return AddressEngineNova().allocate_address(context)
        os_public_network = ec2utils.get_os_public_network(context)
        neutron = clients.neutron(context)

        with common.OnCrashCleaner() as cleaner:
            os_floating_ip = {'floating_network_id': os_public_network['id']}
            try:
                os_floating_ip = neutron.create_floatingip(
                        {'floatingip': os_floating_ip})
            except neutron_exception.OverQuotaClient:
                raise exception.AddressLimitExceeded()
            os_floating_ip = os_floating_ip['floatingip']
            cleaner.addCleanup(neutron.delete_floatingip, os_floating_ip['id'])

            address = {'os_id': os_floating_ip['id'],
                       'public_ip': os_floating_ip['floating_ip_address']}
            address = db_api.add_item(context, 'eipalloc', address)
        return address, os_floating_ip

    def release_address(self, context, public_ip, allocation_id):
        neutron = clients.neutron(context)
        if public_ip:
            # TODO(ft): implement search in DB layer
            address = next((addr for addr in
                            db_api.get_items(context, 'eipalloc')
                            if addr['public_ip'] == public_ip), None)
            if address and _is_address_valid(context, neutron, address):
                msg = _('You must specify an allocation id when releasing a '
                        'VPC elastic IP address')
                raise exception.InvalidParameterValue(msg)
            return AddressEngineNova().release_address(context,
                                                       public_ip, None)

        address = ec2utils.get_db_item(context, allocation_id)
        if not _is_address_valid(context, neutron, address):
            raise exception.InvalidAllocationIDNotFound(
                id=allocation_id)
 
        ###This will validate db entry and will change entry accordingly
        validate_db(context,address)

       

        if 'network_interface_id' in address:
            raise exception.InvalidIPAddressInUse(
                ip_address=address['public_ip'])

        with common.OnCrashCleaner() as cleaner:
            db_api.delete_item(context, address['id'])
            cleaner.addCleanup(db_api.restore_item, context,
                               'eipalloc', address)
            try:
                neutron.delete_floatingip(address['os_id'])
            except neutron_exception.NotFound:
                pass

    def associate_address(self, context, public_ip=None, instance_id=None,
                          allocation_id=None, network_interface_id=None,
                          private_ip_address=None):
        #                  private_ip_address=None, allow_reassociation=False):
        instance_network_interfaces = []
        if instance_id:
            # TODO(ft): implement search in DB layer
            for eni in db_api.get_items(context, 'eni'):
                if instance_id and eni.get('instance_id') == instance_id:
                    instance_network_interfaces.append(eni)
                    #Reverting changes for JNT-149
                    #Bellow changes done for JNT-149.It will restrict user to allocate more than one
                    # RJIL IP for an instances
                    eni_id= eni['id']  #store network interface id
                    #for epi in db_api.get_items(context, 'eipalloc'):
                        #If network interface Id will be present in eipalloc data then it will throw the exception
                    #    if 'network_interface_id' in  epi:
                    #        if eni_id == epi['network_interface_id']:
                    #            raise exception.AlreadyRjilIPAssociated(public_ip=epi['public_ip'], allocation_id=epi['id'])

        neutron = clients.neutron(context)
        if public_ip:
            if instance_network_interfaces:
                msg = _('You must specify an allocation id when mapping '
                        'an address to a VPC instance')
                raise exception.InvalidParameterCombination(msg)
            # TODO(ft): implement search in DB layer
            address = next((addr for addr in db_api.get_items(context,
                                                              'eipalloc')
                            if addr['public_ip'] == public_ip), None)
            if address and _is_address_valid(context, neutron, address):
                msg = _("The address '%(public_ip)s' does not belong to you.")
                raise exception.AuthFailure(msg % {'public_ip': public_ip})

            # NOTE(ft): in fact only the first two parameters are used to
            # associate an address in EC2 Classic mode. Other parameters are
            # sent to validate their emptiness in one place
            return AddressEngineNova().associate_address(
                    context, public_ip=public_ip, instance_id=instance_id,
                    allocation_id=allocation_id,
                    network_interface_id=network_interface_id,
                    private_ip_address=private_ip_address)
            #        allow_reassociation=allow_reassociation)

        if instance_id:
            if not instance_network_interfaces:
                # NOTE(ft): check the instance exists
                ec2utils.get_db_item(context, instance_id)
                msg = _('You must specify an IP address when mapping '
                        'to a non-VPC instance')
                raise exception.InvalidParameterCombination(msg)
            if len(instance_network_interfaces) > 1:
                raise exception.InvalidInstanceId(instance_id=instance_id)
            network_interface = instance_network_interfaces[0]
        else:
            network_interface = ec2utils.get_db_item(context,
                                                     network_interface_id)
        if not private_ip_address:
            private_ip_address = network_interface['private_ip_address']

        address = ec2utils.get_db_item(context, allocation_id)
        if not _is_address_valid(context, neutron, address):
            raise exception.InvalidAllocationIDNotFound(
                id=allocation_id)
        
        ###This will validate db entry and will change entry accordingly
        validate_db(context,address)        


        if address.get('network_interface_id') == network_interface['id']:
            # NOTE(ft): idempotent call
            pass
        #elif address.get('network_interface_id') and not allow_reassociation:
        elif address.get('network_interface_id'):
            msg = _('resource %(eipalloc_id)s is already associated with '
                    'associate-id %(eipassoc_id)s')
            msg = msg % {'eipalloc_id': allocation_id,
                         'eipassoc_id': ec2utils.change_ec2_id_kind(
                                                address['id'], 'eipassoc')}
            raise exception.ResourceAlreadyAssociated(msg)
        else:
            #Check if disassociate is done or not.
            if 'status' in address :
                if get_rt_ip_status(address['public_ip']) == Status[0] :
                    msg = _('address %(eipassoc_id)s is still disassociating. Retry in few seconds ')
                    msg = msg % { 'eipassoc_id': ec2utils.change_ec2_id_kind(
                                                address['id'], 'eipassoc') }
                    raise exception.AddressStillDisassociating(msg)
                
            internet_gateways = (
                internet_gateway_api.describe_internet_gateways(
                    context,
                    filter=[{'name': 'attachment.vpc-id',
                             'value': [network_interface['vpc_id']]}])
                ['internetGatewaySet'])
            #if len(internet_gateways) == 0:
            #    msg = _('Network %(vpc_id)s is not attached to any internet '
            #            'gateway') % {'vpc_id': network_interface['vpc_id']}
            #    raise exception.GatewayNotAttached(msg)

            with common.OnCrashCleaner() as cleaner:
                _associate_address_item(context, address,
                                        network_interface['id'],
                                        private_ip_address)
                cleaner.addCleanup(_disassociate_address_item, context,
                                   address)

                os_floating_ip = {'port_id': network_interface['os_id'],
                                  'fixed_ip_address': private_ip_address}
                neutron.update_floatingip(address['os_id'],
                                          {'floatingip': os_floating_ip})
        # TODO(ft): generate unique association id for each act of association
        return ec2utils.change_ec2_id_kind(address['id'], 'eipassoc')

    def disassociate_address(self, context, public_ip=None,
                             association_id=None):
        neutron = clients.neutron(context)
        if public_ip:
            # TODO(ft): implement search in DB layer
            address = next((addr for addr in db_api.get_items(context,
                                                              'eipalloc')
                            if addr['public_ip'] == public_ip), None)
            if address and _is_address_valid(context, neutron, address):
                msg = _('You must specify an association id when unmapping '
                        'an address from a VPC instance')
                raise exception.InvalidParameterValue(msg)
            # NOTE(ft): association_id is unused in EC2 Classic mode, but it's
            # passed there to validate its emptiness in one place
            return AddressEngineNova().disassociate_address(
                    context, public_ip=public_ip,
                    association_id=association_id)

        address = db_api.get_item_by_id(
            context, ec2utils.change_ec2_id_kind(association_id, 'eipalloc'))
        if address is None or not _is_address_valid(context, neutron, address):
            raise exception.InvalidAssociationIDNotFound(
                    id=association_id)
        if 'network_interface_id' in address:
            with common.OnCrashCleaner() as cleaner:
                network_interface_id = address['network_interface_id']
                private_ip_address = address['private_ip_address']
                status = _disassociate_address_item(context, address)
                cleaner.addCleanup(_associate_address_item, context, address,
                                   network_interface_id, private_ip_address)

                neutron.update_floatingip(address['os_id'],
                                          {'floatingip': {'port_id': None}})
                
                return status

    def get_os_floating_ips(self, context):
        neutron = clients.neutron(context)
        return neutron.list_floatingips(
            tenant_id=context.project_id)['floatingips']

    def get_os_ports(self, context):
        neutron = clients.neutron(context)
        return neutron.list_ports(tenant_id=context.project_id)['ports']


class AddressEngineNova(object):
    # TODO(ft): check that parameters unused in EC2 Classic mode are not
    # specified

    def allocate_address(self, context, domain=None):
        nova = clients.nova(context)
        try:
            nova_floating_ip = nova.floating_ips.create()
        except nova_exception.Forbidden:
            raise exception.AddressLimitExceeded()
        return None, self.convert_ips_to_neutron_format(context,
                                                        [nova_floating_ip])[0]

    def release_address(self, context, public_ip, allocation_id):
        nova = clients.nova(context)
        nova.floating_ips.delete(self.get_nova_ip_by_public_ip(context,
                                                               public_ip).id)

    def associate_address(self, context, public_ip=None, instance_id=None,
                          allocation_id=None, network_interface_id=None,
                          private_ip_address=None):
        #                  private_ip_address=None, allow_reassociation=False):
        os_instance_id = ec2utils.get_db_item(context, instance_id)['os_id']
        # NOTE(ft): check the public IP exists to raise AWS exception otherwise
        self.get_nova_ip_by_public_ip(context, public_ip)
        nova = clients.nova(context)
        nova.servers.add_floating_ip(os_instance_id, public_ip)
        return None

    def disassociate_address(self, context, public_ip=None,
                             association_id=None):
        os_instance_id = self.get_nova_ip_by_public_ip(context,
                                                       public_ip).instance_id
        if os_instance_id:
            nova = clients.nova(context)
            nova.servers.remove_floating_ip(os_instance_id, public_ip)
        return None

    def get_os_floating_ips(self, context):
        nova = clients.nova(context)
        return self.convert_ips_to_neutron_format(context,
                                                  nova.floating_ips.list())

    def convert_ips_to_neutron_format(self, context, nova_ips):
        neutron_ips = []
        for nova_ip in nova_ips:
            neutron_ips.append({'id': nova_ip.id,
                                'floating_ip_address': nova_ip.ip,
                                'fixed_ip_address': nova_ip.fixed_ip,
                                'instance_id': nova_ip.instance_id})
        return neutron_ips

    def get_os_ports(self, context):
        return []

    def get_nova_ip_by_public_ip(self, context, public_ip,
                                 nova_floating_ips=None):
        if nova_floating_ips is None:
            nova = clients.nova(context)
            nova_floating_ips = nova.floating_ips.list()
        nova_ip = next((ip for ip in nova_floating_ips
                        if ip.ip == public_ip), None)
        if nova_ip is None:
            msg = _( "The address '%(public_ip)s' does not belong to you.")
            raise exception.AuthFailure(msg % {'public_ip': public_ip})
        return nova_ip


address_engine = get_address_engine()
