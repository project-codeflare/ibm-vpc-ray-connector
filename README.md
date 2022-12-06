# ibm-vpc-ray-connector enables Ray cluster to be deployed over IBM VPC infrastructure

## Setup
> Use of python virtual environment, e.g. [virtualenv](https://virtualenv.pypa.io/en/latest), is greatly encouraged to avoid installing Python packages globally, which could break system tools or other projects

1. Install ibm-vpc-ray-connector on your machine

```
pip install ibm-vpc-ray-connector
```

2. Configure ibm vpc
    * [Generate API KEY](https://www.ibm.com/docs/en/spectrumvirtualizecl/8.1.3?topic=installing-creating-api-key)

    * [Create/Update security group](https://cloud.ibm.com/docs/vpc?topic=vpc-configuring-the-security-group) to have SSH, Redis and Ray Dashboard ports open: 22, 8265 and 6379

3. Create cluster config file

    * Use interactive `ibm-ray-config` config tool to generate cluster.yaml configuration file
    ```
    pip install ibm-ray-config
    ibm-ray-config -o cluster.yaml
    ```
## Usage
- Use generated file to bring ray cluster up, e.g `ray up cluster.yaml`.

* After finished, find cluster head node and worker nodes ips:

```
ray get-head-ip cluster.yaml
ray get-worker-ips cluster.yaml
```

* To get status of the cluster

```
ray status --address PUBLIC_HEAD_IP:6379
```

* Use browser to open ray dashboard on PUBLIC_HEAD_IP:8265. Alternatively use `ray dashboard` to forward ray cluster dashboard to your localhost. 

* Submit example task `ray submit cluster.yaml templates/example.py`

## Logs
Logs for the node_provider can be found under `/tmp/connector_logs/`.  
Logs of all levels will be written to `connector_logs`.  
The default log level for console output is `INFO`.   