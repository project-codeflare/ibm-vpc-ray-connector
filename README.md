# Gen2-connector enables Ray cluster to be deployed over IBM Gen2 infrastructure

1. Install [Ray](https://github.com/ray-project/ray) release 1.4.1 using `pip install ray[default]==1.4.1`

2. Install gen2-connector on your machine

```
pip install gen2-connector
```

3. Configure ibm vpc
    * [Generate API KEY](https://www.ibm.com/docs/en/spectrumvirtualizecl/8.1.3?topic=installing-creating-api-key)

    * [Create/Update security group](https://cloud.ibm.com/docs/vpc?topic=vpc-configuring-the-security-group) to have SSH, Redis and Ray Dashboard ports open: 22, 8265 and 6379

4. Create cluster config file

    * Use interactive `vpc-config` tool to generate cluster.yaml configuration file
    ```
    vpc-config --iam-api-key ${IAM_API_KEY} --format ray --filename cluster.yaml
    ```
    
    * Select security group from previous step when prompted

    * The output of the `vpc-config` is a cluster config yaml file, e.g
    ```
    =================================================
    Cluster config file: /tmp/tmpkf0dztfk.yaml
    =================================================
    ```

    * Alternatively, update cluster config manually based on [defaults.yaml](templates/defaults.yaml)
    
6. Use generated file to bring ray cluster up, e.g

```ray up /tmp/tmpkf0dztfk.yaml```

* After finished, find cluster head node and worker nodes ips:

```
ray get-head-ip /tmp/tmpkf0dztfk.yaml
ray get-worker-ips /tmp/tmpkf0dztfk.yaml
```

* To get status of the cluster

```
ray status --address PUBLIC_HEAD_IP:6379
```

* Use browser to open ray dashboard on PUBLIC_HEAD_IP:8265. Alternatively use `ray dashboard` to forward ray cluster dashboard to your localhost. 

* Submit example task `ray submit /tmp/tmpkf0dztfk.yaml templates/example.py`
