"""One parser process per job keeps CPU work and format failures off the ASGI loop."""
import json
import sys
from pathlib import Path

from .core import AppError
from .parsers import parse_document


def main():
    try:
        result = parse_document(Path(sys.argv[1]).read_bytes(), sys.argv[2])
        encoded = json.dumps({'data': result}, ensure_ascii=False).encode('utf-8')
        if len(encoded) > int(sys.argv[4]):
            raise AppError(413, 'PARSER_OUTPUT_TOO_LARGE', '解析产物超过大小预算')
    except AppError as error:
        encoded = json.dumps({'error': {'code': error.code, 'message': error.message, 'status': error.status_code}}, ensure_ascii=False).encode('utf-8')
    Path(sys.argv[3]).write_bytes(encoded)


if __name__ == '__main__':
    main()
