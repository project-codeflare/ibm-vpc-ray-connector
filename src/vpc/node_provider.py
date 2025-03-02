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

import concurrent.futures as cf
import inspect
import json
import logging
import re
import socket
import threading
import time
import os
from pprint import pprint
from pathlib import Path
from uuid import uuid4

from ibm_cloud_sdk_core import ApiException
from ibm_cloud_sdk_core.authenticators import IAMAuthenticator
from ibm_vpc import VpcV1
from typing import Any, Dict, List, Optional

from ray.autoscaler._private.cli_logger import cli_logger
from ray.autoscaler._private.util import hash_runtime_conf
from ray.autoscaler.node_provider import NodeProvider
from ray.autoscaler.tags import (
    NODE_KIND_HEAD,
    NODE_KIND_WORKER,
    TAG_RAY_CLUSTER_NAME,
    TAG_RAY_NODE_KIND,
    TAG_RAY_NODE_NAME,
)

LOGS_FOLDER = "/tmp/connector_logs/"   # this node_provider's logs location. 
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

INSTANCE_NAME_UUID_LEN = 8
INSTANCE_NAME_MAX_LEN = 64
PENDING_TIMEOUT = 120  #  a node of this age that isn't running, will be removed from the cluster.    
PROFILE_NAME_DEFAULT = "cx2-2x4"
VOLUME_TIER_NAME_DEFAULT = "general-purpose"
RAY_RECYCLABLE = "ray-recyclable"  # identifies resources created by this package. these resources are deleted alongside the node.  
VPC_TAGS = ".ray-vpc-tags"


def _get_vpc_client(endpoint, authenticator):
    """
    Creates an IBM VPC python-sdk instance
    """
    ibm_vpc_client = VpcV1(version = '2022-06-30', authenticator=authenticator)
    ibm_vpc_client.set_service_url(endpoint + "/v1")

    return ibm_vpc_client


class IBMVPCNodeProvider(NodeProvider):
    """Node Provider for IBM VPC

    This provider assumes ray-cluster.yaml contains IBM Cloud credentials and
    all necessary ibm vpc details including existing VPC id, VS image, security
    group...etc.

    Most convenient way to generate config file is to use `ibm-ray-config` config
    tool. Install it using `pip install ibm-ray-config`, run it with --pr flag,
    choose `Ray IBM VPC` and follow interactive wizard.

    Currently, instance tagging is implemented using internal cache

    To communicate with head node from outside cluster private network,
    `use_hybrid_ips` set to True. Then, floating (external) ip allocated to
    cluster head node, while worker nodes are provisioned with private ips only.
    """

    def log_in_out(func):
        """Tracing decorator for debugging purposes"""

        def decorated_func(*args, **kwargs):
            name = func.__name__
            logger.debug(
                f"\n\nEnter {name} from {inspect.stack()[0][3]} "
                f"{inspect.stack()[1][3]} {inspect.stack()[2][3]} with args: "
                f"entered with args:\n{pprint(args)} and kwargs {pprint(kwargs)}"
            )
            try:
                result = func(*args, **kwargs)
                logger.debug(
                    f"Leave {name} from {inspect.stack()[1][3]} with result "
                    f"Func Result:{pprint(result)}\n\n"
                )
            except Exception:
                cli_logger.error(f"Error in {name}")
                raise
            return result

        return decorated_func

    def _load_tags(self):
        """if local tags cache (file) exists (cluster is restarting), cache is loaded and deleted nodes are filtered away. result is dumped to local cache.
        otherwise, initializes the in memory and local storage tags cache with the head's cluster tags.     """
        self.nodes_tags = {}

        self.tags_file = Path.home() / VPC_TAGS

        # local tags cache exists from former runs 
        if self.tags_file.is_file():
            all_tags = json.loads(self.tags_file.read_text())
            tags = all_tags.get(self.cluster_name, {})

            # filters instances that were deleted since the last time the head node was up
            for instance_id, instance_tags in tags.items():
                try: 
                    instance = self.ibm_vpc_client.get_instance(instance_id).get_result()
                    if instance and instance["status"] not in ["deleting","failed"]:
                        self.nodes_tags[instance_id] = instance_tags
                    else:
                        logger.warning(
                            f"cached instance {instance_id} is not in a valid state, \
                                and will be removed from cache"
                        )
                except Exception as e:
                    cli_logger.warning(instance_id)
                    if e.message == "Instance not found":
                        logger.error(
                            f"cached instance {instance_id} not found, \
                                will be removed from cache"
                        )
            self.set_node_tags(None, None)  # dump in-memory cache to local cache (file). 
 
        else:
            name = socket.gethostname() # returns the instance's (VSI) name 
            logger.debug(f"Check if {name} is HEAD")

            if self._get_node_type(name) == NODE_KIND_HEAD: 
                logger.debug(f"{name} is HEAD")
                node = self.ibm_vpc_client.list_instances(name=name).get_result()[
                    "instances"
                ]
                if node:
                    logger.debug(f"{name} is node in vpc")

                    ray_bootstrap_config = Path.home() / "ray_bootstrap_config.yaml"  # reads the cluster's config file (an initialized defaults.yaml)
                    config = json.loads(ray_bootstrap_config.read_text())
                    (runtime_hash, mounts_contents_hash) = hash_runtime_conf(
                        config["file_mounts"], None, config
                    )

                    head_tags = {
                        TAG_RAY_NODE_KIND: NODE_KIND_HEAD,
                        "ray-node-name": name,
                        "ray-node-status": "up-to-date",
                        "ray-cluster-name": self.cluster_name,
                        "ray-user-node-type": config["head_node_type"],
                        "ray-runtime-config": runtime_hash,
                        "ray-file-mounts-contents": mounts_contents_hash,
                    }

                    logger.debug(f"Setting HEAD node tags {head_tags}")
                    self.set_node_tags(node[0]["id"], head_tags)

    def __init__(self, provider_config, cluster_name):
        """
        Args:
            provider_config (dict): containing the provider segment of the cluster's config file (see defaults.yaml).
                initialized as an instance variable of the parent class, hence can accessed via self.
            cluster_name(str): value of cluster_name within the cluster's config file. 

        """
        NodeProvider.__init__(self, provider_config, cluster_name)

        _configure_logger()

        self.lock = threading.RLock()
        self.endpoint = self.provider_config["endpoint"]
        self.iam_api_key = self.provider_config["iam_api_key"]
        self.iam_endpoint = self.provider_config.get("iam_endpoint")

        self.ibm_vpc_client = _get_vpc_client(
            self.endpoint, IAMAuthenticator(self.iam_api_key, url=self.iam_endpoint)
        )

        self._load_tags()

        self.cached_nodes = {} # Cache of starting/running/pending(below PENDING_TIMEOUT) nodes. {node_id:node_data}.
        self.pending_nodes = {} # cache of the nodes created, but not yet tagged and running. {node_id:time_of_creation}.
        self.deleted_nodes = [] # ids of nodes scheduled for deletion.

        # if cache_stopped_nodes == true, nodes will be stopped instead of deleted to accommodate future rise in demand  
        self.cache_stopped_nodes = provider_config.get("cache_stopped_nodes", True)

    def _get_node_type(self, name):
        if f"{self.cluster_name}-{NODE_KIND_WORKER}" in name:
            return NODE_KIND_WORKER
        elif f"{self.cluster_name}-{NODE_KIND_HEAD}" in name:
            return NODE_KIND_HEAD

    def _get_nodes_by_tags(self, filters):
        """ 
        returns list of nodes who's tags are matching the specified filters. 
        Args:
            filters(dict):  specified conditions to filter nodes by.
        """

        nodes = []
        # either no filters were specified or the only filter is the type of the node
        if not filters or list(filters.keys()) == [TAG_RAY_NODE_KIND]:
            result = self.ibm_vpc_client.list_instances().get_result()
            instances = result["instances"]
            while result.get("next"):
                start = result["next"]["href"].split("start=")[1]
                result = self.ibm_vpc_client.list_instances(start=start).get_result()
                instances.extend(result["instances"])

            for instance in instances:
                kind = self._get_node_type(instance["name"])
                if kind and instance["id"] not in self.deleted_nodes:
                    if not filters or kind == filters[TAG_RAY_NODE_KIND]:
                        nodes.append(instance)
                        with self.lock:
                            node_cache = self.nodes_tags.setdefault(instance["id"], {})
                            node_cache.update(
                                {
                                    TAG_RAY_CLUSTER_NAME: self.cluster_name,
                                    TAG_RAY_NODE_KIND: kind,
                                }
                            )
        else:  # match filters specified
            with self.lock:
                tags = self.nodes_tags.copy()

                for node_id, node_tags in tags.items():

                    # filter by tags
                    if not all(item in node_tags.items() for item in filters.items()):
                        logger.debug(
                            f"specified filter {filters} doesn't match node"
                            f"tags {node_tags}"
                        )
                        continue
                    try:
                        nodes.append(self.ibm_vpc_client.get_instance(node_id).result)
                    except Exception as e:
                        cli_logger.warning(node_id)
                        if e.message == "Instance not found":
                            logger.error(f"failed to find vsi {node_id}, skipping")
                            continue
                        logger.error(f"failed to find instance {node_id}, raising")
                        raise e

        return nodes

    
    def non_terminated_nodes(self, tag_filters)-> List[str]:
        """ 
        returns list of ids of non terminated nodes, matching the specified tags. updates the nodes cache.
        IMPORTANT: this function is called periodically by ray, a fact utilized to refresh the cache (self.cached_nodes).
        Args:
            tag_filters(dict): specified conditions by which nodes will be filtered. 
        """

        res_nodes = []  # collecting valid nodes that are either starting, running or pending (below PENDING_TIMEOUT threshold)

        found_nodes = self._get_nodes_by_tags(tag_filters)

        for node in found_nodes:

            # check if node scheduled for delete
            with self.lock:
                if node["id"] in self.deleted_nodes:
                    logger.info(f"{node['id']} scheduled for delete")
                    continue

            # validate instance in correct state
            valid_statuses = ["pending", "starting", "running"]
            if node["status"] not in valid_statuses:
                logger.info(
                    f"{node['id']} status {node['status']}"
                    f" not in {valid_statuses}, skipping"
                )
                continue

            # validate instance not hanging in pending state
            with self.lock:
                if node["id"] in self.pending_nodes:
                    if node["status"] != "running":
                        pending_time = time.time() - self.pending_nodes[node["id"]]
                        logger.debug(f"{node['id']} is pending for {pending_time}")
                        if pending_time > PENDING_TIMEOUT:
                            logger.error(
                                f"pending timeout {PENDING_TIMEOUT} reached, "
                                f"deleting instance {node['id']}"
                            )
                            self._delete_node(node["id"])  # we won't try to restart a failed node even if  
                            continue  # avoid adding the node to cached_nodes and move on the next one
                    else:
                        self.pending_nodes.pop(node["id"], None)

            # if node is a head node, validate a floating ip is bound to it 
            if self._get_node_type(node["name"]) == NODE_KIND_HEAD:
                nic_id = node["network_interfaces"][0]["id"]

                # find head node external ip
                res = self.ibm_vpc_client.list_instance_network_interface_floating_ips(
                    node["id"], nic_id
                ).get_result()

                floating_ips = res["floating_ips"]
                if len(floating_ips) == 0:
                    # not adding a head node that's missing floating ip
                    continue
                else:
                    # currently head node always has floating ip
                    # in case floating ip present we want to add it
                    node["floating_ips"] = floating_ips

            res_nodes.append(node)

        for node in res_nodes:
            self.cached_nodes[node["id"]] = node

        return [node["id"] for node in res_nodes]

    
    def is_running(self, node_id)-> bool:
        """returns whether a node is in status running"""
        with self.lock:
            node = self._get_cached_node(node_id)
            logger.debug(f"""node: {node_id} is_running? {node["status"] == "running"}""")
            return node["status"] == "running"

    
    def is_terminated(self, node_id)-> bool:
        """returns True if a node is either not recorded or not in any valid status."""
        with self.lock:
            try:
                node = self._get_cached_node(node_id)
                logger.debug(f"""node: {node_id} is_terminated? {node["status"] not in ["running", "starting", "pending"]}""")
                return node["status"] not in ["running", "starting", "pending"]
            except Exception:
                return True

    
    def node_tags(self, node_id)-> Dict[str, str]:
        """returns tags of specified node id """

        with self.lock:
            return self.nodes_tags.get(node_id, {})

    def _get_hybrid_ip(self, node_id):
        """return external ip for head and private ips for workers"""
    
        node = self._get_cached_node(node_id)
        node_type = self._get_node_type(node["name"])
        if node_type == NODE_KIND_HEAD:
            fip = node.get("floating_ips")
            if fip:
                return fip[0]["address"]

            node = self._get_node(node_id)
            fip = node.get("floating_ips")
            if fip:
                return fip[0]["address"]
        else:
            return self.internal_ip(node_id)
  
    def external_ip(self, node_id)-> str:
        """returns head node's public ip. 
        if use_hybrid_ips==true in cluster's config file, returns the ip address of a node based on its 'Kind'."""

        with self.lock:
            if self.provider_config.get("use_hybrid_ips"):
                return self._get_hybrid_ip(node_id)

            node = self._get_cached_node(node_id)
            fip = node.get("floating_ips")
            if fip:
                return fip[0]["address"]

    def internal_ip(self, node_id)-> str:
        """returns the worker's node private ip address"""
        node = self._get_cached_node(node_id)

        try:
            primary_ip = node["network_interfaces"][0].get("primary_ip")['address']
            if primary_ip is None:
                node = self._get_node(node_id)
        except Exception:
            node = self._get_node(node_id)

        return node["network_interfaces"][0].get("primary_ip")['address']

    def set_node_tags(self, node_id, tags) -> None:
        """
        updates local (file) tags cache. updates in memory cache if node_id and tags are specified 
        Args:
            node_id(str): id of the node provided by the cloud provider at creation.
            tags(dict): specified conditions by which nodes will be filtered.
        """
        with self.lock:
            # update in-memory cache
            if node_id and tags:
                node_cache = self.nodes_tags.setdefault(node_id, {})
                node_cache.update(tags)

            # dump in-memory cache to file
            self.tags_file = Path.home() / VPC_TAGS

            all_tags = {}
            if self.tags_file.is_file():
                all_tags = json.loads(self.tags_file.read_text())
            all_tags[self.cluster_name] = self.nodes_tags
            self.tags_file.write_text(json.dumps(all_tags))

    def _get_instance_data(self, name):
        """Returns instance (node) information matching the specified name"""

        instances_data = self.ibm_vpc_client.list_instances(name=name).get_result()
        if len(instances_data["instances"]) > 0:
            return instances_data["instances"][0]
        return None

    def _create_instance(self, name, base_config):
        """
        Creates a new VM instance with the specified name, based on the provided base_config configuration dictionary 
        Args:
            name(str): name of the instance.
            base_config(dict): specific node relevant data. node type segment of the cluster's config file, e.g. ray_head_default. 
        """

        logger.info("Creating new VM instance {}".format(name))

        security_group_identity_model = {"id": base_config["security_group_id"]}
        subnet_identity_model = {"id": base_config["subnet_id"]}
        primary_network_interface = {
            "name": "eth0",
            "subnet": subnet_identity_model,
            "security_groups": [security_group_identity_model],
        }

        boot_volume_profile = {
            "capacity": base_config.get("boot_volume_capacity", 100),
            "name": f"boot-volume-{uuid4().hex[:4]}",
            "profile": {
                "name": base_config.get("volume_tier_name", VOLUME_TIER_NAME_DEFAULT)
            },
        }

        boot_volume_attachment = {
            "delete_volume_on_instance_delete": True,
            "volume": boot_volume_profile,
        }

        key_identity_model = {"id": base_config["key_id"]}
        profile_name = base_config.get("instance_profile_name", PROFILE_NAME_DEFAULT)

        instance_prototype = {}
        instance_prototype["name"] = name
        instance_prototype["keys"] = [key_identity_model]
        instance_prototype["profile"] = {"name": profile_name}
        instance_prototype["resource_group"] = {"id": base_config["resource_group_id"]}
        instance_prototype["vpc"] = {"id": base_config["vpc_id"]}
        instance_prototype["image"] = {"id": base_config["image_id"]}

        instance_prototype["zone"] = {"name": self.provider_config["zone_name"]}
        instance_prototype["boot_volume_attachment"] = boot_volume_attachment
        instance_prototype["primary_network_interface"] = primary_network_interface

        if "user_data" in base_config:
            instance_prototype["user_data"] = base_config["user_data"]

        if "metadata_service" in base_config:
            instance_prototype["metadata_service"] = base_config["metadata_service"]

        try:
            with self.lock:
                resp = self.ibm_vpc_client.create_instance(instance_prototype)
        except ApiException as e:
            if e.code == 400 and "already exists" in e.message:
                return self._get_instance_data(name)
            elif e.code == 400 and "over quota" in e.message:
                cli_logger.error(
                    "Create VM instance {} failed due to quota limit".format(name)
                )
            else:
                cli_logger.error(
                    "Create VM instance {} failed with status code {}".format(
                        name, str(e.code)
                    )
                )
            raise e

        logger.info("VM instance {} created successfully ".format(name))
        return resp.result

    def _create_floating_ip(self, base_config):
        """returns unbound floating IP address. Creates a new ip if none were found in the config file. 
        Args:
            base_config(dict): specific node relevant data. node type segment of the cluster's config file, e.g. ray_head_default.
        """

        if base_config.get("head_ip"):
            for ip in self.ibm_vpc_client.list_floating_ips().get_result()["floating_ips"]:
                if ip["address"] == base_config["head_ip"]:
                    return ip

        floating_ip_name = "{}-{}".format(RAY_RECYCLABLE, uuid4().hex[:4])
        # create a new floating ip 
        logger.info("Creating floating IP {}".format(floating_ip_name))
        floating_ip_prototype = {}
        floating_ip_prototype["name"] = floating_ip_name
        floating_ip_prototype["zone"] = {"name": self.provider_config["zone_name"]}
        floating_ip_prototype["resource_group"] = {
            "id": base_config["resource_group_id"]
        }
        response = self.ibm_vpc_client.create_floating_ip(floating_ip_prototype)
        floating_ip_data = response.result

        return floating_ip_data

    def _attach_floating_ip(self, instance, fip_data):
        """
        attach a floating ip to the network interface of an instance
        Args: 
            instance(dict): extensive data of a node.
            fip_data(dict): floating ip data. 
        """

        fip = fip_data["address"]
        fip_id = fip_data["id"]

        logger.debug(
            "Attaching floating IP {} to VM instance {}".format(fip, instance["id"])
        )

        # check if floating ip is not attached yet
        inst_p_nic = instance["primary_network_interface"]

        if inst_p_nic["primary_ip"] and inst_p_nic["id"] == fip_id:
            # floating ip already attached. do nothing
            logger.debug("Floating IP {} already attached to eth0".format(fip))
        else:
            # attach floating ip
            self.ibm_vpc_client.add_instance_network_interface_floating_ip(
                instance["id"], instance["network_interfaces"][0]["id"], fip_id
            )

    def _stopped_nodes(self, tags):
        """
        returns stopped nodes of type specified in tags. TAG_RAY_NODE_KIND is a mandatory field. 
        Args:
            tags(dict): set of conditions nodes will be filtered by. 
        """

        filter = {
            TAG_RAY_CLUSTER_NAME: self.cluster_name,
            TAG_RAY_NODE_KIND: tags[TAG_RAY_NODE_KIND],
        }

        nodes = []
        for node_id in self.nodes_tags:
            try:
                node_tags = self.nodes_tags[node_id]
                if all(item in node_tags.items() for item in filter.items()):
                    node = self.ibm_vpc_client.get_instance(node_id).result
                    state = node["status"]
                    if state in ["stopped", "stopping"]:
                        nodes.append(node)
            except Exception as e:
                cli_logger.warning(node_id)
                if e.message == "Instance not found":
                    continue
                raise e
        return nodes

    def _create_node(self, base_config, tags):
        """
        returns dict {instance_id:instance_data} of newly created node. updates tags cache.
        creates a node in the following format: ray-{cluster_name}-{node_type}-{uuid}
        
        Args:
            base_config(dict): specific node relevant data. node type segment of the cluster's config file, e.g. ray_head_default.
            tags(dict): set of conditions nodes will be filtered by.
        """
        
        name_tag = tags[TAG_RAY_NODE_NAME]
        if len(name_tag) > INSTANCE_NAME_MAX_LEN - INSTANCE_NAME_UUID_LEN - 1:
            logger.error(f"node name: {name_tag} is longer then {INSTANCE_NAME_MAX_LEN - INSTANCE_NAME_UUID_LEN - 1} characters")
            raise Exception("Invalid node name: length check failed")

        pattern = re.compile("^([a-z]|[a-z][-a-z0-9]*[a-z0-9])$")  # IBM VPC VSI pattern requirement  
        res = pattern.match(name_tag)
        
        if not res:
            logger.error(f"node name {name_tag} doesn't match the naming pattern `[a-z]|[a-z][-a-z0-9]*[a-z0-9]` for IBM VPC instance ")
            raise Exception("Invalid node name: pattern check for IBM VPC instance failed")

        # append instance name with uuid
        name = "{name_tag}-{uuid}".format(
            name_tag=name_tag, uuid=uuid4().hex[:INSTANCE_NAME_UUID_LEN]
        )

        # create instance in vpc
        instance = self._create_instance(name, base_config)

        # record creation time. used to discover hanging nodes.
        with self.lock:
            self.pending_nodes[instance["id"]] = time.time()   

        tags[TAG_RAY_CLUSTER_NAME] = self.cluster_name
        tags[TAG_RAY_NODE_NAME] = name
        self.set_node_tags(instance["id"], tags)

        # currently always creating public ip for head node
        if self._get_node_type(name) == NODE_KIND_HEAD:
            fip_data = self._create_floating_ip(base_config)
            self._attach_floating_ip(instance, fip_data)

        return {instance["id"]: instance}

    # @log_in_out
    def create_node(self, base_config, tags, count) -> None:
        """
        returns dict of {instance_id:instance_data} of nodes. creates 'count' number of nodes.
        if enabled in base_config, tries to re-run stopped nodes before creation of new instances.
        
        Args:
            base_config(dict): specific node relevant data. node type segment of the cluster's config file, e.g. ray_head_default.
                                a template shared by all nodes when creating multiple nodes (count>1). 
            tags(dict): set of conditions nodes will be filtered by.
            count(int): number of nodes to create. 

        """
        logger.debug(f"""create_node:\ncount:{count}\ntags:{pprint(tags)}\nbase_config:{pprint(base_config)}\n """)
        stopped_nodes_dict = {}
        futures = []

        # Try to reuse previously stopped nodes with compatible configs
        if self.cache_stopped_nodes:
            stopped_nodes = self._stopped_nodes(tags)
            stopped_nodes_ids = [n["id"] for n in stopped_nodes]
            stopped_nodes_dict = {n["id"]: n for n in stopped_nodes}

            if stopped_nodes:
                cli_logger.print(
                    f"Reusing nodes {stopped_nodes_ids}. "
                    "To disable reuse, set `cache_stopped_nodes: False` "
                    "under `provider` in the cluster configuration."
                )

            for node in stopped_nodes:
                logger.info(f"Starting instance {node['id']}")
                self.ibm_vpc_client.create_instance_action(node["id"], "start")

            time.sleep(1)

            for node_id in stopped_nodes_ids:
                self.set_node_tags(node_id, tags)
                with self.lock:
                    if node_id in self.deleted_nodes:
                        self.deleted_nodes.remove(node_id)

            count -= len(stopped_nodes_ids)

        created_nodes_dict = {}

        # create multiple instances concurrently
        if count:
            with cf.ThreadPoolExecutor(count) as ex:
                for i in range(count):
                    futures.append(ex.submit(self._create_node, base_config, tags))

            for future in cf.as_completed(futures):
                created_node = future.result()
                created_nodes_dict.update(created_node)

        all_created_nodes = stopped_nodes_dict
        all_created_nodes.update(created_nodes_dict)

        # this sleep is required due to race condition with non_terminated_nodes
        # called in separate thread by autoscaler. not a lost as anyway the vsi
        # operating system takes time to start. can be removed after
        # https://github.com/ray-project/ray/issues/28150 resolved
        time.sleep(5)

        return all_created_nodes

    def _delete_node(self, node_id):
        """deletes specified instance. if it's a head node delete its IPs if it was created by Ray. updates caches. """

        logger.debug(f"in _delete_node with id {node_id}")
        try:
            floating_ips = []

            # get a node's (head node) floating ip
            try:  
                node = self._get_node(node_id)
                floating_ips = node.get("floating_ips", [])
            except Exception:
                pass

            self.ibm_vpc_client.delete_instance(node_id)

            with self.lock:
                # drop node tags
                self.nodes_tags.pop(node_id, None)
                self.pending_nodes.pop(node["id"], None)
                self.deleted_nodes.append(node_id)
                self.cached_nodes.pop(node_id, None)

                # calling set_node_tags with None will dump self.nodes_tags cache to file
                self.set_node_tags(None, None)

            # delete all ips attached to head node if they were created by this module.
            for ip in floating_ips: 
                if ip["name"].startswith(RAY_RECYCLABLE):
                    self.ibm_vpc_client.delete_floating_ip(ip["id"])
        except ApiException as e:
            if e.code == 404:
                pass
            else:
                raise e

    def terminate_nodes(self, node_ids)-> Optional[Dict[str, Any]]:

        if not node_ids:
            return

        futures = []
        with cf.ThreadPoolExecutor(len(node_ids)) as ex:
            for node_id in node_ids:
                logger.debug("NodeProvider: {}: Terminating node".format(node_id))
                futures.append(ex.submit(self.terminate_node, node_id))

        for future in cf.as_completed(futures):
            future.result()
            
    @log_in_out
    def terminate_node(self, node_id)-> Optional[Dict[str, Any]]:
        """Deletes the VM instance and the associated volume. 
        if cache_stopped_nodes==true in the cluster config file, nodes are stopped instead. """

        logger.info("Deleting VM instance {}".format(node_id))

        try:
            if self.cache_stopped_nodes:
                cli_logger.print(
                    f"Stopping instance {node_id}. To terminate instead, "
                    "set `cache_stopped_nodes: False` "
                    "under `provider` in the cluster configuration"
                )

                self.ibm_vpc_client.create_instance_action(node_id, "stop")
            else:
                cli_logger.print(f"Terminating instance {node_id}")
                self._delete_node(node_id)

        except ApiException as e:
            if e.code == 404:
                pass
            else:
                raise e

    def _get_node(self, node_id):
        """Refresh and get info for this node, updating the cache."""
        self.non_terminated_nodes({})  # Side effect: updates cache

        if node_id in self.cached_nodes:
            return self.cached_nodes[node_id]

        try:
            node = self.ibm_vpc_client.get_instance(node_id).get_result()
            with self.lock:
                self.cached_nodes[node_id] = node
            return node
        except Exception as e:
            logger.error(f"failed to get instance with id {node_id}")
            raise e

    def _get_cached_node(self, node_id):
        """Return node info from cache if possible, otherwise fetches it."""
        if node_id in self.cached_nodes:
            return self.cached_nodes[node_id]

        return self._get_node(node_id)

    @staticmethod
    def bootstrap_config(cluster_config)-> Dict[str, Any]:
        return cluster_config

def _configure_logger():
    """
    Configures the logger of this module for console output and file output
    logs of level DEBUG and higher will be directed to file under LOGS_FOLDER.
    logs of level INFO and higher will be directed to console output. 
        This level can be modified via setting an environment variable LOGLEVEL.
    """

    if not os.path.exists(LOGS_FOLDER):
        os.mkdir(LOGS_FOLDER)
    logs_path =  LOGS_FOLDER + time.strftime("%Y-%m-%d--%H-%M-%S")

    file_formatter   = logging.Formatter('%(asctime)s %(levelname)-8s %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    file_handler = logging.FileHandler(logs_path)
    file_handler.setFormatter(file_formatter)
    file_handler.setLevel(logging.DEBUG)

    console_output_handler = logging.StreamHandler()
    console_output_handler.setFormatter(file_formatter)
    console_output_handler.setLevel(logging.INFO)

    logger.addHandler(file_handler)
    logger.addHandler(console_output_handler)    
