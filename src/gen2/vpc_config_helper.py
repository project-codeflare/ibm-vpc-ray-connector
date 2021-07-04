import click
import tempfile
import inquirer
import yaml 
import os.path as path

from ibm_cloud_sdk_core import ApiException
from ibm_cloud_sdk_core.authenticators import IAMAuthenticator
from ibm_platform_services import GlobalSearchV2, GlobalTaggingV1
from ibm_vpc import VpcV1

def get_option_from_list(msg, choices, default=None, choice_key='name', do_nothing=None):
    if(len(choices) == 0):
        error_msg = f"There no option for {msg}"
        print(error_msg)
        raise Exception(error_msg)

    if(len(choices) == 1 and not do_nothing):
        return(choices[0])

    choices_keys = [choice[choice_key] for choice in choices]
    if do_nothing:
        choices_keys.insert(0, do_nothing)

    questions = [
            inquirer.List('answer',
                message=msg,
                choices=choices_keys,
                default=default,
            ),]
    answers = inquirer.prompt(questions)
    
    # now find the object by name in the list
    if answers['answer'] == do_nothing:
        return None
    else:
        return next((x for x in choices if x[choice_key] == answers['answer']), None)

def find_name_id(objects, msg, obj_id=None, obj_name=None, default=None):
    if obj_id:
        # just validating that obj exists
        obj_name = next((obj['name'] for obj in objects if obj['id'] == obj_id), None)
        if not obj_name:
            raise Exception(f'Object with specified id {obj_id} not found')
    if obj_name:
        obj_id = next((obj['id'] for obj in objects if obj['name'] == obj_name), None)

    if not obj_id and not obj_name:
        obj = get_option_from_list(msg, objects, default=default)
        obj_id = obj['id']
        obj_name = obj['name']

    return obj_name, obj_id

def convert_to_ray(data):
    result = {'provider': {}, 'node_config': {}}

    result['provider']['zone_name'] = data['zone']
    result['provider']['endpoint'] = data['endpoint']
    result['provider']['iam_api_key'] = data['iam_api_key'] #TODO:

    result['node_config']['vpc_id'] = data['vpc_id']
    result['node_config']['resource_group_id'] = data['resource_group_id'] 
    result['node_config']['security_group_id'] = data['sec_group_id']
    result['node_config']['subnet_id'] = data['subnet_id']
    result['node_config']['key_id'] = data['ssh_key_id']
    result['node_config']['image_id'] = data['image_id']
    result['node_config']['instance_profile_name'] = data['instance_profile_name']
    result['node_config']['volume_tier_name'] = data['volume_profile_name']
    if data.get('head_ip'):
        result['node_config']['head_ip'] = data['head_ip']

    return result

def convert_to_lithops(data):
    result = {'ibm_vpc': {}}

    result['ibm_vpc']['endpoint'] = data['endpoint']
    result['ibm_vpc']['vpc_id'] = data['vpc_id']
    result['ibm_vpc']['resource_group_id'] = data['resource_group_id']
    result['ibm_vpc']['security_group_id'] = data['sec_group_id']
    result['ibm_vpc']['subnet_id'] = data['subnet_id']
    result['ibm_vpc']['key_id'] = data['ssh_key_id']
    result['ibm_vpc']['image_id'] = data['image_id']
    result['ibm_vpc']['zone_name'] = data['zone'] 

    return result

def print_to_file(format, filename, result):
    if format:
        print(f"converting results to {format} format")

    if not filename:
        filename = tempfile.mkstemp(suffix = '.yaml')[1]

    if format == 'ray':
        result = convert_to_ray(result)
        
        defaults_config_path = path.abspath(path.join(__file__ ,"../../etc/gen2-connector/defaults.yaml"))

        with open(defaults_config_path) as f: #TODO: find path
            config = yaml.safe_load(f)
            config['provider'].update(result['provider'])

            if config.get('available_node_types'):
                for available_node_type in config['available_node_types']:
                    config['available_node_types'][available_node_type]['node_config'].update(result['node_config'])
            else:
                config['available_node_types'] = {'ray_head_default': {'node_config': result['node_config']}}

            with open(filename, 'w') as outfile:
                yaml.dump(config,  outfile, default_flow_style=False)
    elif format == 'lithops':
        result = convert_to_lithops(result)
     
        with open(filename, 'w') as outfile:
            yaml.dump(result,  outfile, default_flow_style=False)
    else:
        with open(filename, 'w') as outfile:
            yaml.dump(result,  outfile, default_flow_style=False)

    print("\n=================================================")
    print(f"Results stored in {filename}")
    print("=================================================")
         

@click.command()
@click.option('--filename', '-f', help='Filename to save configurations')
@click.option('--iam_api_key', required=True, help='IAM_API_KEY')
@click.option('--region', help='region')
@click.option('--zone', help='availability zone name')
@click.option('--vpc_id', help='vpc id')
@click.option('--sec_group_id', help='security group id')
@click.option('--subnet_id', help='subnet id')
@click.option('--ssh_key_id', help='ssh key id')
@click.option('--image_id', help='image id')
@click.option('--instance_profile_name', help='instance profile name')
@click.option('--volume_profile_name', default='general-purpose', help='volume profile name')
@click.option('--head_ip', help='head node floating ip')
@click.option('--format', type=click.Choice(['lithops', 'ray']), help='if not specified will print plain text')
def builder(filename, iam_api_key, region, zone, vpc_id, sec_group_id, subnet_id, ssh_key_id, image_id, instance_profile_name, volume_profile_name, head_ip, format):
    authenticator = IAMAuthenticator(iam_api_key)
    ibm_vpc_client = VpcV1('2021-01-19', authenticator=authenticator)


    result = {}

    # find region and endpoint
    endpoint = None
    regions_objects = ibm_vpc_client.list_regions().get_result()['regions']
    if not region:
        region_obj = get_option_from_list("Choose region:", regions_objects)
        region = region_obj['name']
        endpoint = region_obj['endpoint']
    else:
        # just need to find endpoint
        region_obj = next((obj for obj in regions_objects if obj['name'] == region), None)
        endpoint = region_obj['endpoint']

    ibm_vpc_client.set_service_url(endpoint  + '/v1')

    result['region'] = region
    result['endpoint'] = endpoint

    # find availability zone
    if not zone:
        zone_obj = get_option_from_list("Choose availability zone:", ibm_vpc_client.list_region_zones(region).get_result()['zones'])
        zone = zone_obj['name']

    result['zone'] = zone

    vpc_name = ''
    vpc_obj = None
    vpc_objects = ibm_vpc_client.list_vpcs().get_result()['vpcs']
    if not vpc_id:
        vpc_obj = get_option_from_list("Choose vpc:", vpc_objects)
        vpc_id = vpc_obj['id']
        vpc_name = vpc_obj['name']
    else:
        for vpc_obj in vpc_objects:
            if vpc_obj['id'] == vpc_id:
                vpc_name = vpc_obj['name']
                break

        if not vpc_name:
            raise Exception(f'Vpc with specified id {vpc_id} not found')
            
    result['vpc_name'] = vpc_name
    result['vpc_id'] = vpc_id

    result['resource_group_id'] = vpc_obj['resource_group']['id']
    result['resource_group_name'] = vpc_obj['resource_group']['name']

    sec_group_objects = ibm_vpc_client.list_security_groups().get_result()['security_groups']
    sec_group_name, sec_group_id = find_name_id(sec_group_objects, "Choose security group", obj_id=sec_group_id)
    result['sec_group_name'] = sec_group_name
    result['sec_group_id'] = sec_group_id

    if head_ip:
        for ip in ibm_vpc_client.list_floating_ips().get_result()['floating_ips']:
            if ip['address'] == head_ip:
                if ip.get('target'):
                    raise Exception(f"Specified head ip {head_ip} occupied, please choose another or let ray create a new one")
                else:
                    result['head_ip'] = head_ip
                    break
    else:
        floating_ips = ibm_vpc_client.list_floating_ips().get_result()['floating_ips']
        free_floating_ips = [x for x in floating_ips if not x.get('target')]
        if free_floating_ips:
            ALLOCATE_NEW_FLOATING_IP = 'Allocate new floating ip'
            head_ip_obj = get_option_from_list("Choose head ip", free_floating_ips, choice_key='address', do_nothing=ALLOCATE_NEW_FLOATING_IP)
            if head_ip_obj:
                result['head_ip'] = head_ip_obj['address']

    subnet_objects = ibm_vpc_client.list_subnets().get_result()['subnets']
    subnet_name, subnet_id = find_name_id(subnet_objects, "Choose subnet", obj_id=subnet_id)
    result['subnet_name'] = subnet_name
    result['subnet_id'] = subnet_id

    ssh_key_objects = ibm_vpc_client.list_keys().get_result()['keys']
    ssh_key_name, ssh_key_id = find_name_id(ssh_key_objects, 'Choose ssh key', obj_id=ssh_key_id)
    result['ssh_key_name'] = ssh_key_name
    result['ssh_key_id'] = ssh_key_id

    image_objects = ibm_vpc_client.list_images().get_result()['images']
    image_name, image_id = find_name_id(image_objects, 'Choose VM image', obj_id=image_id, default='ibm-ubuntu-20-04-minimal-amd64-2')
    result['image_name'] = image_name
    result['image_id'] = image_id

    instance_profile_objects = ibm_vpc_client.list_instance_profiles().get_result()['profiles']
    if instance_profile_name:
        # just validate
        instance_profile_name = next((obj['name'] for obj in instance_profile_objects if obj['name'] == instance_profile_name), None)
        if not instance_profile_name:
            raise Exception(f"specified instance_profile_name {instance_profile_name} not found")
    else:
        obj = get_option_from_list('Carefully choose instance profile, please refer to https://cloud.ibm.com/docs/vpc?topic=vpc-profiles', instance_profile_objects)
        instance_profile_name = obj['name']

    result['instance_profile_name'] = instance_profile_name

    result['volume_profile_name'] = volume_profile_name
    result['iam_api_key'] = iam_api_key

    print(f"vpc name: {vpc_name} id: {vpc_id}\nzone: {zone}\nendpoint: {endpoint}\nregion: {region}\nresource group name: {result['resource_group_name']} id: {result['resource_group_id']}\nsecurity group name: {sec_group_name} id: {sec_group_id}\nsubnet name: {subnet_name} id: {subnet_id}\nssh key name: {ssh_key_name} id {ssh_key_id}\nimage name: {image_name} id: {image_id}\n")

    print_to_file(format, filename, result)


if __name__ == '__main__':
    print(f'\nWelcome to vpc config export helper\n')
    builder()
