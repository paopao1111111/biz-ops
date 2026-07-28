from __future__ import annotations

import argparse
import logging

from .auth import HMACAuthenticator
from .config import Config
from .credentials import CredentialStore
from .db import Database
from .executor import Executor
from .http_service import build_server
from .repository import Repository
from .xclient import XAPIClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the isolated X write service")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = Config.load(args.config)
    db = Database(config.database_path)
    db.migrate()
    repository = Repository(db)
    authenticator = HMACAuthenticator(db, config.hmac_secret, config.auth_max_skew_seconds, config.nonce_ttl_seconds)
    credential_store = CredentialStore(config.secrets_path)

    def client_factory(credentials):
        return XAPIClient(
            credentials,
            base_url=config.x_api_base_url,
            proxy_url=config.x_api_proxy_url,
            timeout_seconds=config.x_api_timeout_seconds,
            token_url=config.oauth_token_url,
            refresh_leeway_seconds=config.oauth_refresh_leeway_seconds,
            credential_reloader=credential_store.resolve,
            token_persister=credential_store.update_oauth2_tokens,
        )

    executor = Executor(
        repository, credential_store, client_factory=client_factory,
        verify_ttl_seconds=config.verify_ttl_seconds, lease_seconds=config.operation_lease_seconds,
        tick_seconds=config.executor_tick_seconds, executor_enabled=config.executor_enabled,
    )
    server = build_server(config, repository, authenticator, executor)
    executor.start()
    logging.info("x-write service listening on %s:%s (executor=%s)",
                 config.bind_host, config.bind_port, "on" if config.executor_enabled else "off")
    try:
        server.serve_forever()
    finally:
        executor.stop()
        server.server_close()


if __name__ == "__main__":
    main()
