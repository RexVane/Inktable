import hashlib
import json
import math
import re
import uuid
from datetime import datetime, timezone

_ISO_MS = '%Y-%m-%dT%H:%M:%S.'


class AppError(Exception):
    def __init__(self, status_code, code, message, details=None):
        super().__init__(message)
        self.name = 'AppError'
        self.status_code = status_code
        self.code = code
        self.details = details


def now():
    moment = datetime.now(timezone.utc)
    return moment.strftime(_ISO_MS) + f'{moment.microsecond // 1000:03d}Z'


def gen_id(prefix):
    return f'{prefix}_{uuid.uuid4().hex}'


def hash_bytes(value):
    if isinstance(value, str):
        value = value.encode('utf-8')
    return hashlib.sha256(value).hexdigest()


def stable_json(value):
    def normalize(input_value):
        if isinstance(input_value, list):
            return [normalize(item) for item in input_value]
        if isinstance(input_value, dict):
            return {key: normalize(input_value[key]) for key in sorted(input_value)}
        return input_value
    # JS JSON.stringify: 紧凑分隔符、非 ASCII 原样输出。审计哈希链依赖字节等价。
    return json.dumps(normalize(value), ensure_ascii=False, separators=(',', ':'))


def parse_json(value, fallback=None):
    if value is None or value == '':
        return fallback
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return fallback


def required(value, name):
    if value is None or (isinstance(value, str) and value.strip() == ''):
        raise AppError(400, 'VALIDATION_ERROR', f'{name} 为必填项', {'field': name})
    return value.strip() if isinstance(value, str) else value


def bounded_int(value, fallback, minimum, maximum, name='value'):
    if value is None:
        number = fallback
    else:
        try:
            number = int(value)
        except (ValueError, TypeError):
            number = math.nan
    if not (isinstance(number, int) and not isinstance(number, bool)) or math.isnan(number) or number < minimum or number > maximum:
        raise AppError(400, 'VALIDATION_ERROR', f'{name} 必须是 {minimum} 到 {maximum} 之间的整数', {'field': name})
    return number


def page(query):
    query = query or {}
    return {
        'limit': bounded_int(query.get('limit'), 50, 1, 200, 'limit'),
        'offset': bounded_int(query.get('offset'), 0, 0, 1_000_000, 'offset'),
    }


_SAFE_NAME_RE = re.compile(r'[\x00-\x1f<>:"/\\|?*]')


def safe_name(name):
    return _SAFE_NAME_RE.sub('_', str(name or 'file'))[:180]


_REDACT_KEY_VALUE_RE = re.compile(r'(api[_-]?key|password|secret|token|authorization)\s*[:=]\s*[^\s,;]+', re.IGNORECASE)
_REDACT_CONNECTION_RE = re.compile(r'(postgres(?:ql)?|mysql|mongodb)://[^\s]+', re.IGNORECASE)


def redact(value):
    text = _REDACT_KEY_VALUE_RE.sub(r'\1=[REDACTED]', str(value or ''))
    return _REDACT_CONNECTION_RE.sub('[REDACTED_CONNECTION]', text)


def row_to_object(row):
    if row is None:
        return row
    result = dict(row)
    for key in list(result.keys()):
        value = result[key]
        if key.endswith('_json'):
            result[key[:-5]] = parse_json(value, {})
        if key == 'deleted_at' and value is None:
            del result[key]
    return result
