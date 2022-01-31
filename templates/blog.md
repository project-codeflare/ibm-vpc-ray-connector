# Ray on IBM Cloud VPC

[![N|Solid](https://cldup.com/dTxpPi9lDf.thumb.png)](https://nodesource.com/products/nsolid)

How to run Ray — an open technology for fast and simple distributed computing — on IBM Cloud® VPC.
What is IBM Cloud® VPC?

[IBM Cloud Virtual Private Cloud (VPC)](https://www.ibm.com/cloud/vpc) starts a new generation of IBM Cloud infrastructure. Designed from the ground up for cloud-native workloads, VPC provides a brand-new experience to manage virtual machine-based compute, storage, and networking resources in a private, secure space you define.

Advanced users can benefit from the [Virtual Private Cloud API](https://cloud.ibm.com/apidocs/vpc) that IBM Cloud exposes in order to run technologies like Ray.

# How to run your Ray task on IBM VPC

### Prepare IBM VPC and Ray cluster config 
Although it is not mandatory, but it is highly encouraged to use [python virtual environment](https://docs.python.org/3/tutorial/venv.html)

- Obtain IBM Cloud API Key as described in [the docs](https://cloud.ibm.com/docs/account?topic=account-userapikey)
- Install [lithopscloud](https://github.com/lithops-cloud/lithopscloud/) config tool

    ```bash
    sudo apt install openssh-client
    pip install lithopscloud
    ```
    
- Use `lithopscloud` interactive config tool to setup your VPC and generate `cluster-config.yaml`
    ```bash
    lithopscloud --iam-api-key <IAM_API_KEY> --output-file  cluster-config.yaml

    # Select `Ray Gen2` and then follow the interactive wizard
    [?] Please select a compute backend: Ray Gen2
      Lithops Gen2
      Lithops Cloud Functions
      Lithops Code Engine
    > Ray Gen2
      Local Host

    [?] Choose region: eu-de
      au-syd
      br-sao
      ca-tor
    > eu-de
      eu-gb
      jp-osa
      jp-tok
      us-east
      us-south
      
      .
      .
      .
    ```
    
    Ray will spawn its head and worker nodes on Virtual Server Instances.
    For demo purposes, when inquered to provide requested number of worker nodes, it is recommended to either leave 0 or specify 1 to minimize resources consumption.
    
    ```bash
    [?] Minimum number of worker nodes: 1
    [?] Maximum number of worker nodes: 1
    ```
    Once the the interactive wizard successsfully finishes, you should have Ray cluster-config.yaml file.
    
### Install Ray with IBM VPC node provider
IBM VPC support been added by [implemention of  IBM cloud provider](https://docs.ray.io/en/latest/cluster/cloud.html#additional-cloud-providers) for Ray
- Install [Ray IBM VPC connector](https://github.com/project-codeflare/gen2-connector) 
    ```bash
    pip install gen2-connector
    ```
- Launch your cluster using ray up. It will setup Ray head and worker nodes as Virtual Server Instances (VSI) in your VPC. This setup may take several minutes.
    ```bash
    ray up cluster-config.yaml
    ```
    and wait untill head node setup completed
    
- "Ray up" is finished after head node setup completed. If worker nodes > 0 been configured in the cluster-config.yaml head node will spawn them automatically up to the number specified in the config file. You may observe the setup process in the [IBM Cloud Portal](https://cloud.ibm.com/vpc-ext/compute/vs)


### Now you ready to submit an [example](https://github.com/project-codeflare/gen2-connector/blob/main/templates/example.py) job to Ray cluster

```bash
ray submit example.py
```

### When finished you may desroy your cluster by running
```bash
 ray down cluster-config.yaml
 ```

