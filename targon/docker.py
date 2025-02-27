from time import sleep
import random
from typing import Any, Dict, List, Tuple
import re
import math
import docker
import bittensor as bt
import subprocess
from accelerate.commands import estimate

from docker.models.containers import Container
from docker.types import DeviceRequest
import requests

from targon.config import IMAGE_TAG
from targon.types import Endpoints


def get_gpu_with_space(gpus: List[Tuple[int, int, int]], required: int):
    "[GPU_ID, free, total] in MB"
    bt.logging.info(f"Need: {required}, have: {gpus}")
    
    # find unsused GPUS
    unused = [gpu for gpu in gpus if gpu[1] / gpu[2] > 0.9]

    # find first gpu with enough space
    for gpu in unused:
        if gpu[1] >= required * 1.2:
            return [gpu]
    
    # if we need multiple gpu, only used unused
    total_free = 0
    next_gpus = []
    for gpu in unused:
        total_free += gpu[1]
        next_gpus.append(gpu)
        if total_free > required * 1.2:
            return next_gpus
    return None


def bytes_to_mib(bytes_value):
    mib_value = bytes_value / (1024**2)  # 1024^2 = 1,048,576
    return math.ceil(mib_value)


def estimate_max_size(model_name):
    "Returns size in MiB, what nvidia smi prints"
    try:
        model = estimate.create_empty_model(
            model_name, library_name="transformers", trust_remote_code=False
        )
    except (RuntimeError, OSError) as e:
        library = estimate.check_has_model(e)
        if library != "unknown":
            raise RuntimeError(
                f"Tried to load `{model_name}` with `{library}` but a possible model to load was not found inside the repo."
            )
        return None

    total_size, _ = estimate.calculate_maximum_sizes(model)
    return bytes_to_mib(total_size)


MANIFOLD_VERIFIER = "manifoldlabs/sn4-verifier"


def load_docker():
    client = docker.from_env()
    return client


def get_free_gpus() -> List[Tuple[int, int, int]]:
    "[GPU_ID, free, total] in MB"
    res = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=memory.free,memory.total",
            "--format=csv,noheader",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if res.returncode != 0:
        bt.logging.error(res.stdout.decode("utf-8"))
        raise Exception("Failed to detect nvida gpus")

    lines = [line.split(" ") for line in res.stdout.decode("utf-8").strip().split("\n")]
    gpus = [(i, int(line[0]), int(line[2])) for i, line in enumerate(lines)]
    return gpus


def remove_containers(client):
    containers: List[Container] = client.containers.list(  # type: ignore
        filters={"label": "model"}
    )
    for container in containers:
        model = container.labels.get("model")
        bt.logging.info(f"Removing {container.name}: {model}")
        container.remove(force=True)

def sync_output_checkers(
    client: docker.DockerClient, models: List[str]
) -> Dict[str, Dict[str, Any]]:

    # Get new image hash (if any)
    image_name = f"{MANIFOLD_VERIFIER}:{IMAGE_TAG}"
    try:
        client.images.pull(image_name)  # type: ignore
    except Exception as e:
        bt.logging.error(str(e))
    bt.logging.info(f"Syncing {models}")

    # Remove all containers
    remove_containers(client)
    verification_ports = {}
    used_ports = []
    random.shuffle(models)
    min_port = 5555

    # Clear containers that arent running
    client.containers.prune()

    # Load all models
    bt.logging.info(f"Starting subset of {list(models)}")
    for model in models:
        container_name = re.sub(r"[\W_]", "-", model).lower()

        # Delete if existing and out of date
        existing_containers: List[Container] = client.containers.list(filters={"name": container_name})  # type: ignore
        if len(existing_containers):
            existing_containers[0].remove(force=True)

        # Determine GPU free
        free_gpus = get_free_gpus()
        required_vram = estimate_max_size(model)
        if required_vram is None:
            bt.logging.error(f"Failed to find model {model}")
            continue
        gpus = get_gpu_with_space(free_gpus, required_vram)
        if gpus is None:
            bt.logging.info(f"Not enough space to run {model}")
            continue

        # Find Port
        while min_port in used_ports:
            min_port += 1
        used_ports.append(min_port)

        # Init new container
        bt.logging.info(
            f"Loading {model} on gpu(s) {[gpu[0] for gpu in gpus]}"
        )
        config: Dict[str, Any] = {
            "image": image_name,
            "ports": {f"80/tcp": min_port},
            "environment": [
                f"MODEL={model}",
                f"TENSOR_PARALLEL={len(gpus)}",
            ],
            "volumes": ["/var/targon/huggingface/cache:/root/.cache/huggingface"],
            "runtime": "nvidia",
            "detach": True,
            "ipc_mode": "host",
            "name": container_name,
            "extra_hosts": {"host.docker.internal": "host-gateway"},
            "labels": {"model": str(model), "port": str(min_port)},
            "device_requests": [
                DeviceRequest(
                    device_ids=[str(gpu[0]) for gpu in gpus], capabilities=[["gpu"]]
                )
            ],
        }
        client.containers.run(**config)  # type: ignore
        while True:
            ready = True
            std_model = re.sub(r"[\W_]", "-", model).lower()
            containers: List[Container] = client.containers.list(filters={"name": std_model}, all=True)  # type: ignore
            if not len(containers):
                bt.logging.info(
                    f"Failed starting container {std_model}: Removing from verifiers"
                )
                break
            (container,) = containers
            if container.health == "unhealthy":
                container_logs = container.logs()
                bt.logging.error(
                    f"Failed starting container {std_model}: Removing from verifiers"
                )
                bt.logging.error("---- Verifier Logs ----")
                bt.logging.error(container_logs)
                bt.logging.error("-----------------------")
                break
            if container.health != "healthy":
                bt.logging.info(f"{container.name}: {container.health}")
                ready = False
            if ready:
                verification_ports[model] = {"port": min_port}
                endpoints = requests.get(
                    f"http://localhost:{min_port}/endpoints"
                ).json()
                endpoints = [Endpoints(e.upper()) for e in endpoints]
                verification_ports[model]["endpoints"] = endpoints
                break
            bt.logging.info("Checking again in 5 seconds")
            sleep(5)

    bt.logging.info("Successfully started verifiers")
    bt.logging.info(str(verification_ports))
    if len(list(verification_ports.keys())) == 0:
        bt.logging.error("No verification ports")
        exit()
    return verification_ports
