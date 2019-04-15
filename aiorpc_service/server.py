import sys
import argparse
import logging.config
from pathlib import Path
from typing import List

from aiorpc import start_rpc_server, configure, get_key_enc
from . import get_config, config_logging


logger = logging.getLogger("agent")


def parse_args(argv: List[str]):
    p = argparse.ArgumentParser()
    subparsers = p.add_subparsers(dest='subparser_name')
    server = subparsers.add_parser('server', help='Run web server')
    server.add_argument("--config", required=True, help="Config file path")
    subparsers.add_parser('gen_key', help='Generate new key')
    return p.parse_args(argv[1:])


def main(argv: List[str]) -> int:
    opts = parse_args(argv)
    cfg = get_config(Path(opts.config))
    config_logging(cfg)

    if opts.subparser_name == 'server':
        configure(historic_ops=cfg.historic_ops, historic_ops_cfg=cfg.historic_ops_cfg)
        start_rpc_server(ip=cfg.listen_ip, ssl_cert=cfg.ssl_cert, key=cfg.ssl_key, api_key_enc=cfg.api_key_enc,
                         port=cfg.server_port)
    elif opts.subparser_name == 'gen_key':
        key, enc_key = get_key_enc()
        print(f"Key={key}\nenc_key={enc_key}")
    else:
        assert False, f"Unknown cmd {opts.subparser_name}"
    return 0


if __name__ == "__main__":
    main(sys.argv)
