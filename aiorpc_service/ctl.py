import sys
import uuid
import asyncio
import getpass
import argparse
import subprocess
from pathlib import Path
from typing import List, Any

from koder_utils import SSH, make_secure, make_cert_and_key, rpc_map, b2ssize, read_inventory
from aiorpc import get_http_connection_pool, get_key_enc, IAOIRPCNode

from . import (get_config, get_config_default_path, AIORPCServiceConfig, config_logging, logger, get_certificates,
               get_installation_root, get_config_target_path, get_inventory_path)


SERVICE_FILE_DIR = Path("/lib/systemd/system")


# --------------- SSH BASED CONTROLS FUNCTIONS -------------------------------------------------------------------------


async def stop(service: str, nodes: List[SSH]) -> None:
    logger.info(f"Stopping service {service} on nodes {' '.join(node.node for node in nodes)}")
    await asyncio.gather(*[node.run(["sudo", "systemctl", "stop", service]) for node in nodes])


async def disable(service: str, nodes: List[SSH]) -> None:
    logger.info(f"Disabling service {service} on nodes {' '.join(node.node for node in nodes)}")
    await asyncio.gather(*[node.run(["sudo", "systemctl", "disable", service]) for node in nodes])


async def enable(service: str, nodes: List[SSH]) -> None:
    logger.info(f"Enabling service {service} on nodes {' '.join(node.node for node in nodes)}")
    await asyncio.gather(*[node.run(["sudo", "systemctl", "enable", service]) for node in nodes])


async def start(service: str, nodes: List[SSH]) -> None:
    logger.info(f"Starting service {service} on nodes {' '.join(node.node for node in nodes)}")
    await asyncio.gather(*[node.run(["sudo", "systemctl", "start", service]) for node in nodes])


async def remove(cfg: AIORPCServiceConfig, nodes: List[SSH]):
    logger.info(f"Removing rpc_agent from nodes {' '.join(node.node for node in nodes)}")

    try:
        await disable(cfg.service_name, nodes)
    except subprocess.SubprocessError:
        pass

    try:
        await stop(cfg.service_name, nodes)
    except subprocess.SubprocessError:
        pass

    async def runner(node: SSH) -> None:
        agent_folder = Path(cfg.root)
        service_target = SERVICE_FILE_DIR / cfg.service_name

        await node.run(["sudo", "rm", "--force", str(service_target)])
        await node.run(["sudo", "systemctl", "daemon-reload"])

        logger.info(f"Removing files from {node.node}")

        for folder in (agent_folder, cfg.storage):
            await node.run(["sudo", "rm", "--preserve-root", "--recursive", "--force", str(folder)])

    for node, val in zip(nodes, await asyncio.gather(*map(runner, nodes), return_exceptions=True)):
        if val is not None:
            assert isinstance(val, Exception)
            logger.error(f"Failed on node {node} with message: {val!s}")

    logger.info(f"Removing local config and inventory")
    if get_config_target_path().exists():
        get_config_target_path().unlink()

    if get_inventory_path().exists():
        get_inventory_path().unlink()


async def deploy(cfg: AIORPCServiceConfig, nodes: List[SSH], max_parallel_uploads: int, inventory: List[str]):
    logger.info(f"Start deploying on nodes: {' '.join(inventory)}")

    if cfg.config != get_config_target_path():
        logger.info(f"Copying config to: {get_config_target_path()}")
        get_config_target_path().open("w").write(cfg.config.open().read())

    upload_semaphore = asyncio.Semaphore(max_parallel_uploads if max_parallel_uploads else len(nodes))

    if max_parallel_uploads:
        logger.debug(f"Max uploads is set to {max_parallel_uploads}")

    cfg.secrets.mkdir(mode=0o770, parents=True, exist_ok=True)

    make_secure(cfg.api_key, cfg.api_key_enc)
    api_key, api_enc_key = get_key_enc()

    with cfg.api_key.open('w') as fd:
        fd.write(api_key)

    with cfg.api_key_enc.open('w') as fd:
        fd.write(api_enc_key)

    logger.debug(f"Api keys generated")

    async def runner(node: SSH):
        logger.debug(f"Start deploying node {node.node}")

        await node.run(["sudo", "mkdir", "--parents", cfg.root])
        await node.run(["sudo", "mkdir", "--parents", cfg.storage])

        temp_distr_file = f"/tmp/distribution_{uuid.uuid1()!s}.{cfg.distribution_file.name.split('.')[1]}"

        logger.debug(f"Copying {b2ssize(cfg.distribution_file.stat().st_size)}B of archive to {node.node}")
        async with upload_semaphore:
            await node.copy(cfg.distribution_file, temp_distr_file)

        logger.debug(f"Installing distribution and making dirs on {node.node}")
        # await node.run(["sudo", "tar", "--xz", "--extract", "--directory=" +
        #   str(cfg.root), "--file", temp_distr_file])
        await node.run(["sudo", "bash", temp_distr_file, "--install", str(cfg.root)])
        await node.run(["sudo", "chown", "--recursive", "root.root", cfg.root])
        await node.run(["sudo", "chmod", "--recursive", "o-w", cfg.root])
        await node.run(["sudo", "mkdir", "--parents", cfg.secrets])

        logger.debug(f"Generating certs for {node.node}")
        ssl_cert_file = Path(str(cfg.ssl_cert_templ).replace("[node]", node.node))
        ssl_key_file = cfg.secrets / f'key.{node.node}.tempo'
        make_secure(ssl_cert_file, ssl_key_file)

        await make_cert_and_key(ssl_key_file, ssl_cert_file,
                                f"/C=NN/ST=Some/L=Some/O=aiorpc/OU=aiorpc/CN={node.node}")

        logger.debug(f"Copying certs and keys to {node.node}")
        await node.run(["sudo", "tee", cfg.ssl_cert], input_data=ssl_cert_file.open("rb").read())
        await node.run(["sudo", "tee", cfg.ssl_key], input_data=ssl_key_file.open("rb").read())
        await node.run(["sudo", "tee", cfg.api_key_enc], input_data=api_enc_key.encode("utf8"))
        ssl_key_file.unlink()
        await node.run(["rm", temp_distr_file])

        logger.debug(f"Copying service file to {node.node}")
        service_content = cfg.service.open().read()
        service_content = service_content.replace("{INSTALL}", str(cfg.root))
        service_content = service_content.replace("{CONFIG_PATH}", str(cfg.config))

        await node.run(["sudo", "tee", f"/lib/systemd/system/{cfg.service_name}"],
                       input_data=service_content.encode())
        await node.run(["sudo", "systemctl", "daemon-reload"])
        logger.debug(f"Done with {node.node}")

    await asyncio.gather(*map(runner, nodes))
    await enable(cfg.service_name, nodes)
    await start(cfg.service_name, nodes)

    with get_inventory_path().open("w") as fd:
        fd.write("\n".join(inventory) + "\n")


# --------------- RPC BASED CONTROLS FUNCTIONS -------------------------------------------------------------------------


async def check_node(conn: IAOIRPCNode, hostname: str) -> bool:
    return await conn.proxy.sys.ping("test") == 'test'


async def status(cfg: AIORPCServiceConfig, nodes: List[str]) -> None:
    ssl_certs = get_certificates(cfg.ssl_cert_templ)
    pool_am = get_http_connection_pool(ssl_certs, cfg.api_key.open().read(), cfg.max_conn_per_node, port=cfg.server_port)
    async with pool_am as pool:
        max_node_name_len = max(map(len, nodes))
        async for node_name, res in rpc_map(pool, check_node, nodes):
            if isinstance(res, Exception):
                logger.error(f"{node_name} - error: {res!s}")
            else:
                logger.info("{0:>{1}} {2:>8}".format(node_name, max_node_name_len, "RUN" if res else "NOT RUN"))


def parse_args(argv: List[str]) -> Any:
    try:
        inst_root = get_installation_root()
        cfg_def_path = get_config_default_path()
    except RuntimeError:
        inst_root = None
        cfg_def_path = None

    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    subparsers = parser.add_subparsers(dest='subparser_name')

    deploy_parser = subparsers.add_parser('install', help='Deploy agent on nodes from inventory')
    deploy_parser.add_argument("--max-parallel-uploads", default=0, type=int,
                               help="Max parallel archive uploads to target nodes (default: %(default)s)")
    if inst_root:
        deploy_parser.add_argument("--target", metavar='TARGET_FOLDER', default=inst_root,
                                   help="Path to deploy agent to on target nodes (default: %(default)s)")
    else:
        deploy_parser.add_argument("--target", metavar='TARGET_FOLDER', required=True,
                                   help="Path to deploy agent to on target nodes (default: %(default)s)")

    deploy_parser.add_argument("--inventory", metavar='INVENTORY_FILE', required=True, type=Path,
                               help="Path to file with list of ssh ip/names of ceph nodes")

    stop_parser = subparsers.add_parser('stop', help='Stop daemons')
    start_parser = subparsers.add_parser('start', help='Start daemons')
    remove_parser = subparsers.add_parser('uninstall', help='Remove service')

    for sbp in (deploy_parser, start_parser, stop_parser, remove_parser):
        sbp.add_argument("--ssh-user", metavar='SSH_USER', default=getpass.getuser(),
                         help="SSH user, (default: %(default)s)")

    status_parser = subparsers.add_parser('status', help='Show daemons statuses')
    for sbp in (deploy_parser, start_parser, stop_parser, status_parser, remove_parser):
        if cfg_def_path:
            sbp.add_argument("--config", metavar='CONFIG_FILE', default=cfg_def_path,
                             help="Config file path (default: %(default)s)")
        else:
            sbp.add_argument("--config", metavar='CONFIG_FILE', required=True,
                             help="Config file path (default: %(default)s)")

    return parser.parse_args(argv[1:])


def main(argv: List[str]) -> int:
    opts = parse_args(argv)

    cfg = get_config(opts.config)
    config_logging(cfg, no_persistent=True)

    if opts.subparser_name == 'status':
        inventory = read_inventory(get_inventory_path())
        asyncio.run(status(cfg, inventory))
        return 0

    if opts.subparser_name == 'install':
        inventory = read_inventory(opts.inventory)
    else:
        inventory = read_inventory(get_inventory_path())

    nodes = [SSH(name_or_ip, ssh_user=opts.ssh_user) for name_or_ip in inventory]
    if opts.subparser_name == 'install':
        asyncio.run(deploy(cfg, nodes, max_parallel_uploads=opts.max_parallel_uploads, inventory=inventory))
    elif opts.subparser_name == 'start':
        asyncio.run(start(cfg.service_name, nodes))
    elif opts.subparser_name == 'stop':
        asyncio.run(stop(cfg.service_name, nodes))
    elif opts.subparser_name == 'uninstall':
        asyncio.run(remove(cfg, nodes))
    else:
        assert False, f"Unknown command {opts.subparser_name}"
    return 0


if __name__ == "__main__":
    exit(main(sys.argv))
