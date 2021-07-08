#
# (C) Copyright IBM Corp. 2021
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import os
import click
import tempfile
import inquirer
import yaml 
import os.path as path

from ibm_cloud_sdk_core import ApiException
from ibm_cloud_sdk_core.authenticators import IAMAuthenticator
from ibm_platform_services import GlobalSearchV2, GlobalTaggingV1
from ibm_vpc import VpcV1

from inquirer import errors

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
        return do_nothing
    else:
        return next((x for x in choices if x[choice_key] == answers['answer']), None)

def validate_not_empty(answers, current):
    if not current:
        raise errors.ValidationError('', reason=f"Key name can't be empty")
    return True

def validate_exists(answers, current):
    if not current or not os.path.exists(os.path.abspath(os.path.expanduser(current))):
        raise errors.ValidationError('', reason=f"File {current} doesn't exist")
    return True

def register_ssh_key(ibm_vpc_client, resource_group_id):
    questions = [
      inquirer.Text('keyname', message='Please specify a name for the new key', validate=validate_not_empty)
    ]
    answers = inquirer.prompt(questions)
    keyname = answers['keyname']

    EXISTING_CONTENTS = 'Paste existing public key contents'
    EXISTING_PATH = 'Provide path to existing public key'
    GENERATE_NEW = 'Generate new public key'

    questions = [
            inquirer.List('answer',
                message="Please choose",
                choices=[EXISTING_PATH, EXISTING_CONTENTS, GENERATE_NEW]
            )]

    answers = inquirer.prompt(questions)
    ssh_key_data = ""
    ssh_key_path = None
    if answers["answer"] == EXISTING_CONTENTS:
        print("Registering from file contents")
        ssh_key_data = input("[\033[33m?\033[0m] Please paste the contents of your public ssh key. It should start with ssh-rsa: ")
    elif answers["answer"] == EXISTING_PATH:
        print("Register in vpc existing key from path")
        questions = [
          inquirer.Text("public_key_path", message='Please paste path to your \033[92mpublic\033[0m ssh key', validate=validate_exists)
        ]
        answers = inquirer.prompt(questions)

        with open(answers["public_key_path"], 'r') as file:
            ssh_key_data = file.read()
    else:
        print("generate new keypair")
        filename = f"id.rsa.{keyname}"
        os.system(f'ssh-keygen -b 2048 -t rsa -f {filename} -q -N ""')
        print(f"Generated\n")
        print(f"private key: {os.path.abspath(filename)}")
        print(f"public key {os.path.abspath(filename)}.pub")
        with open(f"{filename}.pub", 'r') as file:
            ssh_key_data = file.read()
        ssh_key_path = os.path.abspath(filename)

    response = ibm_vpc_client.create_key(public_key=ssh_key_data, name=keyname, resource_group={"id": resource_group_id}, type='rsa')
    result = response.get_result()
    return result['name'], result['id'], ssh_key_path

def find_name_id(objects, msg, obj_id=None, obj_name=None, default=None, do_nothing=None):
    if obj_id:
        # just validating that obj exists
        obj_name = next((obj['name'] for obj in objects if obj['id'] == obj_id), None)
        if not obj_name:
            raise Exception(f'Object with specified id {obj_id} not found')
    if obj_name:
        obj_id = next((obj['id'] for obj in objects if obj['name'] == obj_name), None)

    if not obj_id and not obj_name:
        obj = get_option_from_list(msg, objects, default=default, do_nothing=do_nothing)
        if do_nothing and obj == do_nothing:
            return None, None

        obj_id = obj['id']
        obj_name = obj['name']

    return obj_name, obj_id

def convert_to_ray(data):
    result = {'provider': {}, 'node_config': {}}

    result['provider']['region'] = data['region']
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

    result['ssh_key_path'] = data['ssh_key_path']

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

def print_to_file(format, output_file, result, input_file):
    if format:
        print(f"converting results to {format} format")

    if not output_file:
        output_file = tempfile.mkstemp(suffix = '.yaml')[1]

    if format == 'ray':
        result = convert_to_ray(result)
        
        defaults_config_path = input_file or path.abspath(path.join(__file__ ,"../../etc/gen2-connector/defaults.yaml"))
        template_config, _, node_template, _ = get_template_config(input_file)

        with open(defaults_config_path) as f: #TODO: find path
            config = yaml.safe_load(f)
            config['provider'].update(result['provider'])

            default_cluster_name = template_config.get('cluster_name', 'default')
            default_min_workers = node_template.get('min_workers', '0')
            default_max_workers = node_template.get('max_workers', '0')


            question = [
              inquirer.Text('name', message="Cluster name, either leave default or type a new one", default=default_cluster_name),
              inquirer.Text('min_workers', message="Minimum number of worker nodes", default=default_min_workers),
              inquirer.Text('max_workers', message="Maximum number of worker nodes", default=default_max_workers)
            ]

            answers = inquirer.prompt(question)
            config['cluster_name'] = answers['name']
            config['auth']['ssh_private_key'] = result['ssh_key_path']
            config['max_workers'] = int(answers['max_workers'])

            if config.get('available_node_types'):
                for available_node_type in config['available_node_types']:
                    config['available_node_types'][available_node_type]['node_config'].update(result['node_config'])

                    if not result['node_config'].get('head_ip'):
                        config['available_node_types'][available_node_type]['node_config'].pop('head_ip', None)

                    config['available_node_types'][available_node_type]['min_workers'] = int(answers['min_workers'])
                    config['available_node_types'][available_node_type]['max_workers'] = int(answers['max_workers'])
            else:
                config['available_node_types'] = {'ray_head_default': {'node_config': result['node_config']}}
                config['available_node_types']['ray_head_default']['min_workers'] = int(answers['min_workers'])
                config['available_node_types']['ray_head_default']['max_workers'] = int(answers['max_workers'])

            with open(output_file, 'w') as outfile:
                yaml.dump(config,  outfile, default_flow_style=False)
    elif format == 'lithops':
        result = convert_to_lithops(result)
     
        with open(output_file, 'w') as outfile:
            yaml.dump(result,  outfile, default_flow_style=False)
    else:
        with open(output_file, 'w') as outfile:
            yaml.dump(result,  outfile, default_flow_style=False)

    print("\n\n=================================================")
    print(f"\033[92mCluster config file: {output_file}\033[0m")
    print("=================================================")
         
# currently supported only for ray
def get_template_config(input_file):
    template_config = {}
    provider_template = {}
    node_template = {}
    node_config = {}

    if input_file:
        with open(input_file) as f:
             template_config = yaml.safe_load(f)
             provider_template = template_config.get('provider', {})
             node_configs = tuple(template_config.get('available_node_types', {}).values())

             if node_configs:
                 node_template = node_configs[0]
                 node_config = node_configs[0].get('node_config', {})

    return template_config, provider_template, node_template, node_config

def find_default(template_dict, objects, name=None, id=None):
    val = None
    if name:
        key = 'name'
        val = template_dict.get(name)
    elif id:
        key='id'
        val = template_dict.get(id)

    if val:
        obj = next((obj for obj in objects if obj[key] == val), None)
        if obj:
            return obj['name']

@click.command()
@click.option('--output_file', '-o', help='Output filename to save configurations')
@click.option('--input_file', '-i', help=f'Template for new configuration, default: {path.abspath(path.join(__file__ ,"../../etc/gen2-connector/defaults.yaml"))}')
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
def builder(output_file, input_file, iam_api_key, region, zone, vpc_id, sec_group_id, subnet_id, ssh_key_id, image_id, instance_profile_name, volume_profile_name, head_ip, format):
    print(f"\n\033[92mWelcome to vpc config export helper\033[0m\n")

    template_config, provider_template, node_template, node_config = get_template_config(input_file) 

    authenticator = IAMAuthenticator(iam_api_key)
    ibm_vpc_client = VpcV1('2021-01-19', authenticator=authenticator)

    result = {}

    # find region and endpoint
    endpoint = None
    regions_objects = ibm_vpc_client.list_regions().get_result()['regions']
    if not region:
        default = find_default(provider_template, regions_objects, name='region')
        region_obj = get_option_from_list("Choose region", regions_objects, default = default)
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
    zones_objects = ibm_vpc_client.list_region_zones(region).get_result()['zones']
    zone_obj = None
    if not zone:
        default = find_default(provider_template, zones_objects, name='zone_name')
        zone_obj = get_option_from_list("Choose availability zone", zones_objects, default = default)
    else:
        zone_obj = next((obj for obj in zones_objects if obj['name'] == zone), None)

    result['zone'] = zone_obj['name']

    vpc_name = ''
    vpc_obj = None
    vpc_objects = ibm_vpc_client.list_vpcs().get_result()['vpcs']
    if not vpc_id:
        default = find_default(node_config, vpc_objects, id='vpc_id')
        vpc_obj = get_option_from_list("Choose vpc", vpc_objects, default = default)
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

    ssh_key_objects = ibm_vpc_client.list_keys().get_result()['keys']
    CREATE_NEW_SSH_KEY = "Register new SSH key in IBM VPC"

    default = find_default(node_config, ssh_key_objects, id='key_id')
    ssh_key_name, ssh_key_id = find_name_id(ssh_key_objects, 'Choose ssh key', obj_id=ssh_key_id, do_nothing=CREATE_NEW_SSH_KEY, default=default)

    ssh_key_path = None
    if not ssh_key_name:
        ssh_key_name, ssh_key_id, ssh_key_path  = register_ssh_key(ibm_vpc_client, result['resource_group_id'])

    if not ssh_key_path:
        questions = [
          inquirer.Text("private_key_path", message=f'Please paste path to \033[92mprivate\033[0m ssh key matching with selected public key {ssh_key_name}', validate=validate_exists, default="~/.ssh/id_rsa")
        ]
        answers = inquirer.prompt(questions)
        ssh_key_path = os.path.abspath(os.path.expanduser(answers["private_key_path"]))

    result['ssh_key_name'] = ssh_key_name
    result['ssh_key_id'] = ssh_key_id
    result['ssh_key_path'] = ssh_key_path

    sec_group_objects = ibm_vpc_client.list_security_groups().get_result()['security_groups']

    default = find_default(node_config, sec_group_objects, id='security_group_id')
    sec_group_name, sec_group_id = find_name_id(sec_group_objects, "Choose security group", obj_id=sec_group_id, default=default)
    result['sec_group_name'] = sec_group_name
    result['sec_group_id'] = sec_group_id

    floating_ips = ibm_vpc_client.list_floating_ips().get_result()['floating_ips']
    if head_ip:
        for ip in floating_ips:
            if ip['address'] == head_ip:
                if ip.get('target'):
                    raise Exception(f"Specified head ip {head_ip} occupied, please choose another or let ray create a new one")
                else:
                    result['head_ip'] = head_ip
                    break
    else:
        free_floating_ips = [x for x in floating_ips if not x.get('target')]
        if free_floating_ips:
            ALLOCATE_NEW_FLOATING_IP = 'Allocate new floating ip'
            head_ip_obj = get_option_from_list("Choose head ip", free_floating_ips, choice_key='address', do_nothing=ALLOCATE_NEW_FLOATING_IP)
            if head_ip_obj and (head_ip_obj != ALLOCATE_NEW_FLOATING_IP):
                result['head_ip'] = head_ip_obj['address']

    subnet_objects = ibm_vpc_client.list_subnets().get_result()['subnets']
    default = find_default(node_config, subnet_objects, id='subnet_id')
    subnet_name, subnet_id = find_name_id(subnet_objects, "Choose subnet", obj_id=subnet_id, default=default)
    result['subnet_name'] = subnet_name
    result['subnet_id'] = subnet_id

    image_objects = ibm_vpc_client.list_images().get_result()['images']
    default = find_default(node_config, image_objects, id='image_id') or 'ibm-ubuntu-20-04-minimal-amd64-2'

    image_name, image_id = find_name_id(image_objects, 'Please choose \033[92mUbuntu\033[0m 20.04 VM image, currently only Ubuntu supported', obj_id=image_id, default=default)
    result['image_name'] = image_name
    result['image_id'] = image_id

    instance_profile_objects = ibm_vpc_client.list_instance_profiles().get_result()['profiles']
    if instance_profile_name:
        # just validate
        instance_profile_name = next((obj['name'] for obj in instance_profile_objects if obj['name'] == instance_profile_name), None)
        if not instance_profile_name:
            raise Exception(f"specified instance_profile_name {instance_profile_name} not found")
    else:
        default = find_default(node_config, instance_profile_objects, name='instance_profile_name')
        obj = get_option_from_list('Carefully choose instance profile, please refer to https://cloud.ibm.com/docs/vpc?topic=vpc-profiles', instance_profile_objects, default=default)
        instance_profile_name = obj['name']

    result['instance_profile_name'] = instance_profile_name

    result['volume_profile_name'] = volume_profile_name
    result['iam_api_key'] = iam_api_key

    print(f"vpc name: {vpc_name} id: {vpc_id}\nzone: {zone_obj['name']}\nendpoint: {endpoint}\nregion: {region}\nresource group name: {result['resource_group_name']} id: {result['resource_group_id']}\nsecurity group name: {sec_group_name} id: {sec_group_id}\nsubnet name: {subnet_name} id: {subnet_id}\nssh key name: {ssh_key_name} id {ssh_key_id}\nimage name: {image_name} id: {image_id}\n")

    print_to_file(format, output_file, result, input_file)


if __name__ == '__main__':
    builder()
