"""Import-only dummy configuration and explicit network/DB denial for offline suites."""
import os
import runpy
import sys
from pathlib import Path
from unittest.mock import patch


def denied(*args, **kwargs):
    raise AssertionError("External I/O is forbidden in the offline acceptance runner")


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: run_offline_acceptance.py SCRIPT [ARGS...]")
    target = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(target.parent))
    os.environ.update(POSTGRES_PASSWORD="offline_import_only_not_a_credential", JWT_SECRET_KEY="offline_import_only_not_a_credential_32chars",
                      POSTGRES_HOST="127.0.0.1", POSTGRES_PORT="1", API_KEY="offline-import-only",
                      ENABLE_DOCMIND="false", ENABLE_WEB_SEARCH="true", BOCHA_API_KEY="offline-import-only")
    import httpx
    import requests
    from sqlalchemy.engine import Engine
    sys.argv = [str(target), *sys.argv[2:]]
    with patch.object(Engine, "connect", denied), patch.object(requests.sessions.Session, "request", denied), \
            patch.object(httpx.Client, "send", denied), patch.object(httpx.AsyncClient, "send", denied):
        runpy.run_path(str(target), run_name="__main__")


if __name__ == "__main__":
    main()
