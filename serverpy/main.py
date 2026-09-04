import uvicorn

from ordo.app import create_app
from ordo.config import resolve_config


def main():
    config = resolve_config()
    app = create_app()
    uvicorn.run(app, host=config['host'], port=config['port'], log_level='info', access_log=False)


if __name__ == '__main__':
    main()
