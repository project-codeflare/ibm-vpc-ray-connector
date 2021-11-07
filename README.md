# Gen2-connector enables Ray cluster to be deployed over IBM Gen2 infrastructure

> Use of python virtual environment, e.g. [virtualenv](https://virtualenv.pypa.io/en/latest) is greatly encouraged, to avoid installing Python packages globally which could break system tools or other projects

1. Install [Ray](https://github.com/ray-project/ray) release 1.8.0 using `pip install ray[default]==1.8.0`

2. Install gen2-connector on your machine

```
pip install gen2-connector
```

3. Configure ibm vpc
    * [Generate API KEY](https://www.ibm.com/docs/en/spectrumvirtualizecl/8.1.3?topic=installing-creating-api-key)

    * [Create/Update security group](https://cloud.ibm.com/docs/vpc?topic=vpc-configuring-the-security-group) to have SSH, Redis and Ray Dashboard ports open: 22, 8265 and 6379

4. Create cluster config file

    * Use interactive `lithopscloud` config tool to generate cluster.yaml configuration file
    ```
    pip install lithopscloud
    lithopscloud -o cluster.yaml
    ```
    
6. Use generated file to bring ray cluster up, e.g

```ray up cluster.yaml```

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
