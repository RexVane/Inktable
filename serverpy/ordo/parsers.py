import csv
import io
import mimetypes
import os
import re
from pathlib import Path

from .core import AppError, hash_bytes

ALLOWED_EXTENSIONS = {'.pdf', '.docx', '.pptx', '.xlsx', '.csv', '.md', '.txt', '.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff', '.webp', '.gif', '.zip', '.tar', '.tar.gz', '.tgz'}
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff', '.webp', '.gif'}
ARCHIVE_EXTENSIONS = {'.zip', '.tar', '.tar.gz', '.tgz'}
MIME_BY_EXT = {
    '.pdf': 'application/pdf', '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', '.csv': 'text/csv',
    '.md': 'text/markdown', '.txt': 'text/plain', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
    '.bmp': 'image/bmp', '.tif': 'image/tiff', '.tiff': 'image/tiff', '.webp': 'image/webp', '.gif': 'image/gif',
    '.zip': 'application/zip', '.tar': 'application/x-tar', '.tar.gz': 'application/gzip', '.tgz': 'application/gzip',
}


def extension_of(filename):
    lower = str(filename or '').lower()
    return '.tar.gz' if lower.endswith('.tar.gz') else Path(lower).suffix


def normalize_text(value):
    value = str(value or '').replace('\r\n', '\n').replace('\r', '\n')
    return ''.join(ch for ch in value if ch in '\n\t' or ord(ch) >= 32)


def estimate_tokens(text):
    source = str(text or '')
    chinese = len(re.findall(r'[\u3400-\u9fff]', source))
    words = len(re.sub(r'[\u3400-\u9fff]', ' ', source).split())
    return max(1, int((chinese * 0.7 + words * 1.3) + 0.999999))


def blocks_from_text(text, locator_type='line', extra=None):
    extra, blocks, lines, buffer, start = extra or {}, [], normalize_text(text).split('\n'), [], 1
    def flush(end):
        nonlocal buffer
        content = '\n'.join(buffer).strip()
        if content:
            blocks.append({'type': 'heading' if re.match(r'^#{1,6}\s', content) else 'code' if content.startswith('```') else 'paragraph', 'contentMd': content, 'contentText': re.sub(r'^#{1,6}\s+', '', content), 'locator': {'type': locator_type, 'start': start, 'end': end, **extra}, 'generatedBy': 'parser', 'confidence': 1, 'warnings': []})
        buffer = []
    for index, line in enumerate(lines, 1):
        if not line.strip():
            flush(index)
            start = index + 1
        else:
            if not buffer:
                start = index
            buffer.append(line)
    flush(len(lines))
    return blocks


def detect_file(buffer, filename):
    extension = extension_of(filename)
    if extension not in ALLOWED_EXTENSIONS:
        raise AppError(415, 'UNSUPPORTED_FORMAT', f'不支持的文件格式: {extension or "未知"}', {'allowed': sorted(ALLOWED_EXTENSIONS)})
    if extension == '.pdf' and not bytes(buffer).startswith(b'%PDF-'):
        raise AppError(422, 'MIME_MISMATCH', '扩展名与文件内容不一致，文件已拒绝', {'extension': extension, 'expected': 'application/pdf'})
    if extension in {'.docx', '.pptx', '.xlsx'} and not bytes(buffer).startswith(b'PK'):
        raise AppError(422, 'MIME_MISMATCH', 'OOXML 文件不是有效 ZIP 容器', {'extension': extension})
    return {'extension': extension, 'mimeType': MIME_BY_EXT.get(extension) or mimetypes.guess_type(filename)[0] or 'application/octet-stream', 'detected': None}


def make_result(filename, blocks, warnings=None, quality_status='publishable', metadata=None):
    normalized = []
    for ordinal, block in enumerate(blocks, 1):
        content_md = normalize_text(block.get('contentMd') or block.get('contentText')).strip()
        content_text = normalize_text(block.get('contentText') or content_md).strip()
        normalized.append({'ordinal': ordinal, 'type': block.get('type', 'paragraph'), 'contentMd': content_md, 'contentText': content_text, 'locator': block.get('locator') or {'type': 'unknown'}, 'tokenCount': estimate_tokens(content_text), 'generatedBy': block.get('generatedBy', 'parser'), 'confidence': float(block.get('confidence', 1)), 'warnings': block.get('warnings', [])})
    title = Path(filename).name
    markdown = ('# ' + title + '\n\n' + '\n\n'.join(block['contentMd'] for block in normalized)).strip() + '\n'
    warnings, metadata = warnings or [], metadata or {}
    quality = {'schemaVersion': 1, 'status': quality_status, 'blockCount': len(normalized), 'warningCount': len(warnings), 'warnings': warnings, 'publishable': quality_status == 'publishable' and bool(normalized)}
    return {'schemaVersion': 1, 'title': title, 'markdown': markdown, 'document': {'schemaVersion': 1, 'title': title, 'metadata': metadata, 'blocks': normalized, 'warnings': warnings}, 'quality': quality, 'blocks': normalized, 'warnings': warnings, 'qualityStatus': quality_status, 'metadata': metadata}


def parse_pdf(buffer, filename):
    try:
        import pymupdf
        document = pymupdf.open(stream=buffer, filetype='pdf')
    except Exception as error:
        raise AppError(422, 'PARSE_FAILED', 'PDF 无法读取或已损坏') from error
    if document.needs_pass:
        document.close()
        raise AppError(422, 'NEEDS_PASSWORD', 'PDF 已加密，需要密码')
    blocks, warnings = [], []
    for number, page in enumerate(document, 1):
        text = normalize_text(page.get_text('text')).strip()
        if text:
            blocks.append({'type': 'paragraph', 'contentMd': f'<!-- page:{number} -->\n{text}', 'contentText': text, 'locator': {'type': 'page', 'page': number}, 'generatedBy': 'pymupdf', 'confidence': .92, 'warnings': []})
        else:
            warnings.append({'code': 'VISUAL_PAGE_REVIEW_REQUIRED', 'page': number, 'message': '页面没有可靠文字层，需要 OCR/VLM Provider'})
    page_count = document.page_count
    document.close()
    return make_result(filename, blocks, warnings, 'publishable' if blocks and not warnings else 'review_required', {'pages': page_count, 'parser': 'pymupdf-lightweight'})


def parse_csv(buffer, filename):
    try:
        text = buffer.decode('utf-8-sig')
        rows = list(csv.reader(io.StringIO(text)))
    except UnicodeDecodeError as error:
        raise AppError(422, 'ENCODING_INVALID', '文本编码无法可靠识别') from error
    except csv.Error as error:
        raise AppError(422, 'PARSE_FAILED', 'CSV 无法读取或已损坏') from error
    content = '\n'.join(' | '.join(row) for row in rows).strip()
    markdown = '\n'.join(','.join('"' + cell.replace('"', '""') + '"' if re.search(r'[",\n]', cell) else cell for cell in row) for row in rows)
    blocks = [{'type': 'table', 'contentMd': f'## Sheet: {Path(filename).stem}\n\n```csv\n{markdown}\n```', 'contentText': content, 'locator': {'type': 'sheet', 'sheet': Path(filename).stem, 'startRow': 1, 'endRow': len(rows)}, 'generatedBy': 'csv', 'confidence': .95, 'warnings': []}] if content else []
    return make_result(filename, blocks, [], 'publishable', {'parser': 'csv', 'sheets': [{'name': Path(filename).stem, 'rows': len(rows), 'columns': max((len(row) for row in rows), default=0)}]})


def _find_text_nodes(value, output=None):
    if output is None:
        output = []
    if isinstance(value, list):
        for item in value:
            _find_text_nodes(item, output)
    elif isinstance(value, dict):
        for key, child in value.items():
            if key in ('a:t', 't') and isinstance(child, str):
                output.append(child)
            else:
                _find_text_nodes(child, output)
    return output


def parse_docx(buffer, filename):
    try:
        import mammoth
        result = mammoth.convert_to_markdown(io.BytesIO(buffer))
    except Exception as error:
        raise AppError(422, 'PARSE_FAILED', 'DOCX 无法读取或已损坏') from error
    text = normalize_text(result.value)
    blocks = blocks_from_text(text, 'paragraph')
    warnings = [{'code': 'DOCX_WARNING', 'message': message.message} for message in result.messages]
    return make_result(filename, blocks, warnings, 'review_required' if warnings else 'publishable', {'parser': 'mammoth'})


def parse_pptx(buffer, filename):
    try:
        import zipfile
        with zipfile.ZipFile(io.BytesIO(buffer)) as archive:
            names = [name for name in archive.namelist()
                     if re.match(r'^ppt/slides/slide\d+\.xml$', name)]
            names.sort(key=lambda name: int(re.search(r'\d+', name).group()))
            slides = [(name, archive.read(name)) for name in names]
    except Exception as error:
        raise AppError(422, 'PARSE_FAILED', 'PPTX 无法读取或已损坏') from error
    blocks = []
    for index, (name, xml) in enumerate(slides):
        try:
            parsed = __import__('xml.etree.ElementTree', fromlist=['ElementTree']).ElementTree.fromstring(xml)
            text = normalize_text('\n'.join(_find_text_nodes(parsed))).strip()
        except Exception:
            text = ''
        if text:
            blocks.append({'type': 'paragraph', 'contentMd': f'## 幻灯片 {index + 1}\n\n{text}',
                           'contentText': text, 'locator': {'type': 'slide', 'slide': index + 1},
                           'generatedBy': 'ooxml-lightweight', 'confidence': .9, 'warnings': []})
    warnings = [{'code': 'VISUAL_ELEMENTS_NOT_INTERPRETED',
                 'message': '图形、SmartArt 与图片已保留在原件中，语义解释需要 VLM Provider'}]
    return make_result(filename, blocks, warnings, 'review_required', {'parser': 'ooxml-lightweight', 'slides': len(slides)})


def parse_xlsx(buffer, filename):
    try:
        import openpyxl
        workbook = openpyxl.load_workbook(io.BytesIO(buffer), data_only=True, read_only=True)
    except Exception as error:
        raise AppError(422, 'PARSE_FAILED', 'XLSX 无法读取或已损坏') from error
    blocks, sheets = [], []
    for sheet in workbook.worksheets:
        rows = [['' if cell is None else str(cell) for cell in row] for row in sheet.iter_rows(values_only=True)]
        text = '\n'.join(' | '.join(row) for row in rows).strip()
        if text:
            csv_text = '\n'.join(','.join('"' + cell.replace('"', '""') + '"' if re.search(r'[",\n]', cell) else cell for cell in row) for row in rows)
            blocks.append({'type': 'table', 'contentMd': f'## Sheet: {sheet.title}\n\n```csv\n{csv_text}\n```', 'contentText': text, 'locator': {'type': 'sheet', 'sheet': sheet.title, 'startRow': 1, 'endRow': len(rows)}, 'generatedBy': 'openpyxl', 'confidence': .95, 'warnings': []})
        sheets.append({'name': sheet.title, 'rows': len(rows), 'columns': max((len(row) for row in rows), default=0)})
    return make_result(filename, blocks, [], 'publishable', {'parser': 'openpyxl', 'sheets': sheets})


def parse_document(buffer, filename):
    detection = detect_file(buffer, filename)
    extension = detection['extension']
    if extension in ARCHIVE_EXTENSIONS:
        raise AppError(422, 'ARCHIVE_REQUIRES_IMPORT', '压缩包必须通过安全导入接口展开')
    if extension in {'.md', '.txt'}:
        try:
            text = buffer.decode('utf-8')
        except UnicodeDecodeError as error:
            raise AppError(422, 'ENCODING_INVALID', '文本编码无法可靠识别') from error
        result = make_result(filename, blocks_from_text(text), [], 'publishable', {'parser': 'ordo-native-text'})
    elif extension == '.csv':
        result = parse_csv(buffer, filename)
    elif extension == '.xlsx':
        result = parse_xlsx(buffer, filename)
    elif extension == '.docx':
        result = parse_docx(buffer, filename)
    elif extension == '.pptx':
        result = parse_pptx(buffer, filename)
    elif extension == '.pdf':
        result = parse_pdf(buffer, filename)
    elif extension in IMAGE_EXTENSIONS:
        warning = {'code': 'OCR_PROVIDER_REQUIRED', 'message': '图片已安全登记，需配置并验证 OCR/VLM Provider 后生成文本知识'}
        result = make_result(filename, [], [warning], 'review_required', {'parser': 'image-metadata', 'contentHash': hash_bytes(buffer)})
    else:
        raise AppError(422, 'PARSER_DEPENDENCY_UNAVAILABLE', f'{extension[1:].upper()} 解析器尚未安装或未启用')
    result['detection'] = detection
    return result
