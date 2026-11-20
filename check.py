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
    need(manifest.get('version') == 1, 'unsupported manifest version')
    need(isinstance(manifest.get('source_sha256'), str) and bool(re.fullmatch(r'[0-9a-f]{64}', manifest.get('source_sha256', ''))), 'invalid source SHA-256')
    pages = manifest.get('pdf_pages')
    need(type(pages) is int and 1 <= pages <= 10000, 'pdf_pages must be 1..10000')
    units = manifest.get('units')
    need(isinstance(units, list) and 1 <= len(units) <= 20000, 'units must contain 1..20000 entries')
    if errors:
        return errors
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
    args = ap.parse_args()
    try:
        manifest = json.loads(read(args.manifest), object_pairs_hook=unique_object)
        errors = validate(manifest, read(args.translation))
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
        print(json.dumps({'result': 'FAIL' if errors else 'PASS_STRUCTURE_ONLY', 'errors': errors,
                          'limitations': 'Does not prove source inventory completeness, meaning, OCR accuracy, or translation quality.'}, ensure_ascii=False, indent=2))
        return 1 if errors else 0
    except (OSError, ValueError, TypeError, RecursionError) as e:
        print(json.dumps({'result': 'ERROR', 'error': str(e)}, ensure_ascii=False))
        return 2

if __name__ == '__main__':
    sys.exit(main())
