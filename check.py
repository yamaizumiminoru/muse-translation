#!/usr/bin/env python3
"""Offline structural checker. A PASS is NOT translation-quality certification."""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

LIMIT = 8 * 1024 * 1024
KINDS = {'heading', 'paragraph', 'list', 'table', 'figure', 'caption', 'equation', 'note', 'reference', 'metadata'}
START = re.compile(r'^<!-- unit:([A-Z][A-Z0-9_-]{0,39}) -->$', re.M)
END = '<!-- /unit -->'
PAGE_KINDS = {'journal': '原誌', 'manuscript': '原稿', 'pdf': 'PDF'}
PAGE_LINE = re.compile(r'^[ \t]*〔(?:原誌|原稿|PDF) p\.[^〕\n]*〕[ \t]*$', re.M)
PAGE_LABEL = re.compile(r'[\w][\w.\-–]{0,31}')
MARKER_LINE = re.compile(r'<!-- unit:[A-Z][A-Z0-9_-]{0,39} -->|<!-- /unit -->')


def expected_pages(manifest):
    n = manifest.get('pdf_pages')
    labels = manifest.get('page_labels')
    kind = manifest.get('page_basis')
    if (type(n) is not int or not 1 <= n <= 10000 or
            not isinstance(kind, str) or kind not in PAGE_KINDS or
            not isinstance(labels, list) or len(labels) != n or
            not all(x is None or isinstance(x, str) and PAGE_LABEL.fullmatch(x) for x in labels)):
        raise ValueError('provide a valid page label or null per physical page and a valid page_basis')
    visible = [x for x in labels if x is not None]
    if len(set(visible)) != len(visible):
        raise ValueError('printed labels must be unique; use null for ambiguous pages')
    if kind == 'pdf' and labels != [str(i) for i in range(1, n + 1)]:
        raise ValueError('physical PDF labels must be 1 through pdf_pages')
    if not visible:
        raise ValueError('use page_basis pdf when no printed labels are identified')
    return [f'〔PDF p.{i}〕' if label is None else f'〔{PAGE_KINDS[kind]} p.{label}〕'
            for i, label in enumerate(labels, 1)]


def reader_filename(manifest):
    name = manifest.get('source_filename')
    if (not isinstance(name, str) or
            not re.fullmatch(r'[^<>:"/\\|?*\x00-\x1f\x7f]+\.pdf', name, re.I)):
        raise ValueError('source_filename must be the original PDF basename')
    return name[:-4] + '日本語訳.md'


def check_page_sequence(wanted, markdown):
    actual = PAGE_LINE.findall(markdown)
    if actual == wanted:
        return []
    for i in range(min(len(actual), len(wanted))):
        if actual[i] != wanted[i]:
            break
    else:
        i = min(len(actual), len(wanted))
    expected = wanted[i] if i < len(wanted) else '(none)'
    observed = actual[i] if i < len(actual) else '(none)'
    return [f'page marker mismatch at position {i + 1}: expected {expected}, '
            f'observed {observed}; total expected {len(wanted)}, observed {len(actual)}']


def filter_reader(markdown):
    return '\n'.join(line for line in markdown.split('\n')
                     if not MARKER_LINE.fullmatch(line))


def validate_reader(manifest, markdown, path):
    expected = filter_reader(markdown).encode('utf-8')
    errors = check_page_sequence(expected_pages(manifest), read(path))
    if path.name != reader_filename(manifest):
        errors.append('reader filename differs from the original PDF basename + 日本語訳.md')
    if path.read_bytes() != expected:
        errors.append('reader bytes differ from QA draft with only unit-marker lines removed')
    return errors


def read(path):
    with Path(path).open('rb') as f:
        data = f.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError('input exceeds 8 MiB')
    return data.decode('utf-8-sig')

def unique_object(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise ValueError('duplicate JSON key: ' + k)
        result[k] = v
    return result

def validate(manifest, markdown):
    errors = []
    def need(ok, msg):
        if not ok:
            errors.append(msg)
    need(isinstance(manifest, dict), 'manifest must be an object')
    if errors:
        return errors
    version = manifest.get('version')
    need(type(version) is int and version in (1, 2), 'unsupported manifest version')
    need(isinstance(manifest.get('source_sha256'), str) and bool(re.fullmatch(r'[0-9a-f]{64}', manifest.get('source_sha256', ''))), 'invalid source SHA-256')
    pages = manifest.get('pdf_pages')
    need(type(pages) is int and 1 <= pages <= 10000, 'pdf_pages must be 1..10000')
    units = manifest.get('units')
    need(isinstance(units, list) and 1 <= len(units) <= 20000, 'units must contain 1..20000 entries')
    if errors:
        return errors
    if version == 2:
        try:
            wanted = expected_pages(manifest)
            reader_filename(manifest)
        except ValueError as e:
            return [str(e)]
        need(not re.search(r'[\ufeff\r]', markdown), 'use UTF-8 without BOM and LF')
    ids = []
    for n, u in enumerate(units):
        if not isinstance(u, dict):
            errors.append('unit must be object: ' + str(n)); continue
        uid = u.get('id')
        need(isinstance(uid, str) and bool(re.fullmatch(r'[A-Z][A-Z0-9_-]{0,39}', uid or '')), 'invalid unit ID')
        ids.append(uid)
        need(u.get('kind') in KINDS, 'invalid kind: ' + str(uid))
        need(type(u.get('page')) is int and 1 <= u.get('page', 0) <= pages, 'invalid PDF page: ' + str(uid))
        need(isinstance(u.get('anchor'), str) and bool(u.get('anchor', '').strip()), 'missing source anchor: ' + str(uid))
        for key in ('preserve', 'note_refs'):
            v = u.get(key)
            need(isinstance(v, list) and all(isinstance(t, str) and t for t in v), 'invalid ' + key + ': ' + str(uid))
        need(u.get('status') == 'readable', 'unreadable source unit: ' + str(uid))
    if errors:
        return errors
    need(len(set(ids)) == len(ids), 'duplicate manifest unit IDs')
    matches = list(START.finditer(markdown))
    need([m.group(1) for m in matches] == ids, 'unit order/count/IDs differ from frozen manifest')
    need(markdown.count(END) == len(ids), 'unit closing marker count differs')
    need(not re.search(r'(?i)\b(?:javascript|vbscript|file|data):', markdown), 'unsafe URL scheme')
    need(not re.search(r'!?\[[^\]\n]*\]\([^\n]*\)|^\[(?!\^)[^\]\n]+\]:\s*\S', markdown, re.M), 'active Markdown links/images not allowed; use inert text')
    clean = START.sub('', markdown).replace(END, '')
    need(not re.search(r'<\s*/?\s*[a-zA-Z][^>]*>', clean), 'raw HTML not allowed')
    need(not re.search(r'\[\[(?:UNREADABLE|UNTRANSLATED|TODO|OMITTED)(?:[^\]]*)\]\]', markdown), 'unresolved translation placeholder')
    bodies = {}
    for i, m in enumerate(matches):
        bound = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        part = markdown[m.end():bound]
        if part.count(END) != 1:
            errors.append('missing/duplicate end: ' + m.group(1)); continue
        body, tail = part.split(END)
        need(bool(body.strip()), 'empty unit: ' + m.group(1))
        need(not tail.strip(), 'untracked text after unit: ' + m.group(1))
        bodies[m.group(1)] = body
    if matches:
        need(not markdown[:matches[0].start()].strip(), 'untracked text before first unit')
    if version == 2:
        errors.extend(check_page_sequence(wanted, markdown))
        first = set()
        for u in units:
            page = u['page']
            body = bodies.get(u['id'], '')
            page_lines = PAGE_LINE.findall(body)
            expected = [wanted[page - 1]] if page not in first else []
            need(page_lines == expected,
                 'page marker must occur once in the first unit for its physical page: ' + u['id'])
            if expected:
                need(body.lstrip('\n').split('\n', 1)[0] == expected[0],
                     'page marker must be the first content line: ' + u['id'])
            first.add(page)
        need(len(first) == len(wanted), 'missing physical page in manifest units')
        need(not re.search(r'<!--|-->', filter_reader(markdown)), 'unexpected comment in reader output')
    note_ids = {u['id'] for u in units if u['kind'] == 'note'}
    for u in units:
        body = bodies.get(u['id'], '')
        for token in u['preserve']:
            need(token in body, 'missing preserved token in ' + u['id'] + ': ' + token)
        expected_refs = u['note_refs']
        actual_refs = re.findall(r'\[\^([A-Z][A-Z0-9_-]*)\](?!:)', body)
        need(actual_refs == expected_refs, 'note references differ: ' + u['id'])
        need(all(ref in note_ids for ref in expected_refs), 'unknown note target: ' + u['id'])
        defs = re.findall(r'^\[\^([A-Z][A-Z0-9_-]*)\]:', body, re.M)
        need(defs == ([u['id']] if u['kind'] == 'note' else []), 'note definition differs: ' + u['id'])
    return errors

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('manifest')
    ap.add_argument('translation')
    ap.add_argument('--pdf', help='optional local source PDF; verify byte hash, not PDF semantics')
    output = ap.add_mutually_exclusive_group()
    output.add_argument('--export', action='store_true', help='write and verify the named reader Markdown next to the QA draft')
    output.add_argument('--reader', type=Path, help='verify an already saved reader Markdown')
    args = ap.parse_args()
    try:
        manifest = json.loads(read(args.manifest), object_pairs_hook=unique_object)
        markdown = read(args.translation)
        errors = validate(manifest, markdown)
        version = manifest.get('version') if isinstance(manifest, dict) else None
        if version == 2:
            if not args.pdf:
                errors.append('v2 verification requires --pdf')
            if not (args.export or args.reader):
                errors.append('v2 requires --export or --reader to verify the deliverable')
            with Path(args.translation).open('rb') as f:
                if f.read(3) == b'\xef\xbb\xbf':
                    errors.append('QA draft must not start with a UTF-8 BOM')
        elif args.export or args.reader:
            errors.append('reader export and verification require manifest v2')
        if args.pdf:
            digest = hashlib.sha256()
            with Path(args.pdf).open('rb') as f:
                total = 0
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk: break
                    total += len(chunk)
                    if total > 256 * 1024 * 1024:
                        raise ValueError('PDF exceeds 256 MiB')
                    digest.update(chunk)
            if digest.hexdigest() != manifest.get('source_sha256'):
                errors.append('source PDF hash mismatch')
        reader = None
        if not errors and version == 2:
            if args.export:
                reader = Path(args.translation).parent / reader_filename(manifest)
                if reader.resolve() in {Path(p).resolve() for p in (args.manifest, args.translation, args.pdf)}:
                    raise ValueError('reader destination conflicts with a source file')
                expected = filter_reader(markdown).encode('utf-8')
                try:
                    with reader.open('xb') as f:
                        f.write(expected)
                except FileExistsError:
                    pass  # Already identical is fine; a different file is never overwritten.
            else:
                reader = args.reader
            errors.extend(validate_reader(manifest, markdown, reader))
        print(json.dumps({'result': 'FAIL' if errors else 'PASS_STRUCTURE_ONLY', 'errors': errors,
                          'reader': reader.name if reader is not None and not errors else None,
                          'limitations': 'Does not prove source inventory completeness, meaning, OCR accuracy, or translation quality.'}, ensure_ascii=True, indent=2))
        return 1 if errors else 0
    except (OSError, ValueError, TypeError, RecursionError) as e:
        print(json.dumps({'result': 'ERROR', 'error': str(e)}, ensure_ascii=True))
        return 2

if __name__ == '__main__':
    sys.exit(main())
