"""One parser process per job keeps CPU work and format failures off the ASGI loop."""
import json
import sys
import zipfile
from pathlib import Path

from .core import AppError
from .parsers import parse_document


def main():
    try:
        source = Path(sys.argv[1])
        if zipfile.is_zipfile(source):
            with zipfile.ZipFile(source) as archive:
                entries = archive.infolist()
                expanded = sum(item.file_size for item in entries)
                if len(entries) > 10000 or expanded > 200 * 1024 * 1024 or expanded > max(source.stat().st_size, 1) * 100:
                    raise AppError(413, 'PARSER_RESOURCE_LIMIT', '压缩文档超过解析资源预算')
        result = parse_document(source.read_bytes(), sys.argv[2])
        encoded = json.dumps({'data': result}, ensure_ascii=False).encode('utf-8')
        if len(encoded) > int(sys.argv[4]):
            raise AppError(413, 'PARSER_OUTPUT_TOO_LARGE', '解析产物超过大小预算')
    except AppError as error:
        encoded = json.dumps({'error': {'code': error.code, 'message': error.message, 'status': error.status_code}}, ensure_ascii=False).encode('utf-8')
    Path(sys.argv[3]).write_bytes(encoded)


if __name__ == '__main__':
    main()
