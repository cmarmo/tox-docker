from typing import Dict

from tox import hookimpl
from tox.action import Action
from tox.config import Config, Parser
from tox.venv import VirtualEnv

from tox_docker.config import ContainerConfig
from tox_docker.plugin import (
    docker_health_check,
    docker_pull,
    docker_run,
    get_env_vars,
    HealthCheckFailed,
    stop_containers,
)
from tox_docker.tox3.config import (
    discover_container_configs,
    EnvRunningContainers,
    parse_container_config,
)
from tox_docker.tox3.log import make_logger

CONTAINER_CONFIGS: Dict[str, ContainerConfig] = {}
ENV_CONTAINERS: EnvRunningContainers = {}


def _newaction(venv: VirtualEnv, message: str) -> Action:
    try:
        # tox 3.7 and later
        return venv.new_action(message)
    except AttributeError:
        return venv.session.newaction(venv, message)


@hookimpl
def tox_configure(config: Config) -> None:
    container_config_names = discover_container_configs(config)

    # validate command line options
    for container_name in config.option.docker_dont_stop:
        if container_name not in container_config_names:
            raise ValueError(
                f"Container {container_name!r} not found (from --docker-dont-stop)"
            )

    container_configs: Dict[str, ContainerConfig] = {}
    for container_name in container_config_names:
        CONTAINER_CONFIGS[container_name] = parse_container_config(
            config, container_name, container_config_names
        )


@hookimpl
def tox_runtest_pre(venv: VirtualEnv) -> None:
    envconfig = venv.envconfig
    container_names = envconfig.docker

    log = make_logger(venv)

    env_container_configs = []

    seen = set()
    for container_name in container_names:
        if container_name not in CONTAINER_CONFIGS:
            raise ValueError(f"Missing [docker:{container_name}] in tox.ini")
        if container_name in seen:
            raise ValueError(f"Container {container_name!r} specified more than once")
        seen.add(container_name)
        env_container_configs.append(CONTAINER_CONFIGS[container_name])

    for container_config in env_container_configs:
        docker_pull(container_config, log)

    ENV_CONTAINERS.setdefault(venv, {})
    containers = ENV_CONTAINERS[venv]

    for container_config in env_container_configs:
        container = docker_run(container_config, containers, log)
        containers[container_config.name] = container

    for container_name, container in containers.items():
        container_config = CONTAINER_CONFIGS[container_name]
        try:
            docker_health_check(container_config, container, log)
        except HealthCheckFailed:
            # TODO: prevent tox from trying tests?
            break

    for container_name, container in containers.items():
        container_config = CONTAINER_CONFIGS[container_name]

        # TODO: for compatibility with tox4, we imitate "update_if_not_present";
        # but ultimately we'd like to use update() for both versions
        for var, val in get_env_vars(container_config, container).items():
            if var not in venv.envconfig.setenv:
                venv.envconfig.setenv[var] = val


@hookimpl
def tox_runtest_post(venv: VirtualEnv) -> None:
    env_containers = ENV_CONTAINERS.get(venv, [])
    containers_and_configs = [
        (CONTAINER_CONFIGS[name], container)
        for name, container in env_containers.items()
    ]
    log = make_logger(venv)
    stop_containers(containers_and_configs, log)


@hookimpl
def tox_addoption(parser: Parser) -> None:
    # necessary to allow the docker= directive in testenv sections
    parser.add_testenv_attribute(
        name="docker",
        type="line-list",
        help="Name of docker images, including tag, to start before the test run",
        default=[],
    )

    # command line flag to keep docker containers running
    parser.add_argument(
        "--docker-dont-stop",
        default=[],
        action="append",
        metavar="CONTAINER",
        help=(
            "If specified, tox-docker will not stop CONTAINER after the test run. "
            "Can be specified multiple times."
        ),
    )
