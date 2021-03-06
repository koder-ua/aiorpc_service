import json
import configparser
import logging.config
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, Any, Optional

from koder_utils import RAttredDict
from aiorpc import get_http_connection_pool, ConnectionPool


files_folder = 'aiorpc_service_files'
distribution = 'distribution.sh'
root_marker = '.install_root'   # should be the same as INSTALL_ROOT_MARKER in ../unpack.sh
logger = logging.getLogger("aiorpc_svc")
service_name = 'aiorpc.service'


# development hack
INSTALL_PATH = Path(__file__).parent


def find_in_top_tree(name: str, _cache: Dict[str, Path] = {}) -> Path:
    if name not in _cache:
        cpath = INSTALL_PATH
        while not (cpath.parent / name).exists():
            if cpath == cpath.parent:
                raise FileExistsError(f"Can't find {name} folder in tree up from {Path(__file__).parent}")
            cpath = cpath.parent
        _cache[name] = cpath.parent / name
    return _cache[name]


def get_files_folder() -> Path:
    return find_in_top_tree(files_folder)


def get_installation_root() -> Path:
    return find_in_top_tree(root_marker).parent


def get_distribution_file_path() -> Path:
    return find_in_top_tree(distribution)


def get_file(name: str) -> Path:
    return get_files_folder() / name


@dataclass
class AIORPCServiceConfig:
    root: Path
    secrets: Path
    log_config: Path
    server_port: int
    log_level: str
    config: Path
    cmd_timeout: int
    storage: Path
    persistent_log: Optional[Path]
    persistent_log_level: Optional[str]
    listen_ip: str
    service_name: str
    service: Path
    ssl_cert: Path
    ssl_key: Path
    api_key_enc: Path
    historic_ops: Path
    historic_ops_cfg: Path
    api_key: Path
    ssl_cert_templ: Path
    max_conn_per_node: int
    max_conn_total: int
    distribution_file: Path
    raw: configparser.ConfigParser
    rraw: Any


def get_config_target_path() -> Path:
    return get_installation_root() / 'aiorpc_config.cfg'


def get_inventory_path() -> Path:
    return get_installation_root() / 'inventory'


def get_config_default_path() -> Optional[Path]:
    root_cfg = get_config_target_path()
    if root_cfg.exists():
        return root_cfg
    else:
        try:
            return get_file('config.cfg')
        except FileExistsError:
            return None


def get_config(path: Path = None) -> AIORPCServiceConfig:
    cfg = configparser.ConfigParser()

    if not path:
        path = get_config_default_path()

    if path is None or not path.exists():
        raise FileExistsError(f"Can't find config file at {path}")

    cfg.read_file(path.open())

    rcfg = RAttredDict(cfg)

    common = rcfg.common
    server = rcfg.server

    path_formatters: Dict[str, Any] = {'root': get_installation_root() if common.root == 'AUTO' else common.root}

    for name, val in [('secrets', common.secrets), ('storage', server.storage)]:
        path_formatters[name] = val.format(**path_formatters)

    def mkpath(val: str) -> Path:
        return Path(val.format(**path_formatters))

    if getattr(server, "persistent_log", None):
        persistent_log = mkpath(server.persistent_log)
        persistent_log_level = server.persistent_log_level
    else:
        persistent_log = None
        persistent_log_level = None

    return AIORPCServiceConfig(
        root=Path(path_formatters['root']),
        secrets=Path(path_formatters['secrets']),

        log_config=get_file("log_config.json"),
        server_port=int(common.server_port),
        log_level=common.log_level,
        config=path,
        cmd_timeout=int(common.cmd_timeout),

        storage=mkpath(server.storage),
        persistent_log=persistent_log,
        persistent_log_level=persistent_log_level,
        listen_ip=server.listen_ip,
        service_name=service_name,
        service=get_file(f"{service_name}"),
        ssl_cert=mkpath(server.ssl_cert),
        ssl_key=mkpath(server.ssl_key),
        api_key_enc=mkpath(server.api_key_enc),
        historic_ops=mkpath(server.historic_ops),
        historic_ops_cfg=mkpath(server.historic_ops_cfg),
        api_key=mkpath(rcfg.client.api_key),
        ssl_cert_templ=mkpath(rcfg.client.ssl_cert_templ),
        max_conn_total=int(rcfg.client.max_conn_total),
        max_conn_per_node=int(rcfg.client.max_conn_per_node),

        distribution_file=mkpath(rcfg.deploy.distribution_file),

        raw=cfg,
        rraw=rcfg
    )


def get_certificates(cert_name_template: Path) -> Dict[str, Path]:
    certificates: Dict[str, Path] = {}

    certs_folder = cert_name_template.parent
    certs_glob = cert_name_template.name

    if not certs_folder.is_dir():
        raise RuntimeError(f"Can't find cert folder at {certs_folder}")

    before_node, after_node = certs_glob.split("[node]")

    for file in certs_folder.glob(certs_glob.replace('[node]', '*')):
        node_name = file.name[len(before_node): -len(after_node)]
        certificates[node_name] = file

    return certificates


def config_logging(cfg: AIORPCServiceConfig, no_persistent: bool = False):
    log_config = json.load(cfg.log_config.open())

    if not cfg.persistent_log or no_persistent:
        del log_config['handlers']['persistent']
    else:
        log_config['handlers']['persistent']['level'] = cfg.persistent_log_level
        if not cfg.persistent_log.parent.exists():
            cfg.persistent_log.parent.mkdir(parents=True)
        log_config['handlers']['persistent']['filename'] = str(cfg.persistent_log)
        for lcfg in log_config['loggers'].values():
            lcfg['handlers'].append('persistent')

    log_config['handlers']['console']['level'] = cfg.log_level
    logging.config.dictConfig(log_config)


def get_http_conn_pool_from_cfg(cfg: AIORPCServiceConfig = None) -> ConnectionPool:
    if cfg is None:
        cfg = get_config()

    certs = get_certificates(cfg.ssl_cert_templ)
    return get_http_connection_pool(certs, cfg.api_key.open().read(),
                                    max_conn_per_node=cfg.max_conn_per_node,
                                    max_conn_total=cfg.max_conn_total)