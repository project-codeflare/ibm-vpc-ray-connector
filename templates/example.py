import ray
import time

ray.init(address='auto')

@ray.remote(num_cpus=1)
def f(x):
    time.sleep(0.1)
    return x * x

futures = [f.remote(i) for i in range(20)]
print(ray.get(futures))
