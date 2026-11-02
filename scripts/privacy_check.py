#!/usr/bin/env python3
"""Check tracked content without printing secrets. Heuristic, not a security guarantee."""
import argparse
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PATTERNS = {
    'private key': rb'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----',
    'API token': rb'\b(?:sk-[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b',
    'JWT': rb'\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{12,}\b',
    'personal home path': rb'/Users/(?!Shared/)[A-Za-z0-9_.-]+/',
    'machine temporary path': rb'/(?:private/)?var/folders/[A-Za-z0-9_-]+/',
    'literal credential': rb'["\x27](?:access_token|refresh_token|id_token|password)["\x27]\s*:\s*["\x27][A-Za-z0-9_./+-]{24,}["\x27]',
}
EMAIL = re.compile(rb'\b[\w.+-]+@([\w.-]+\.[A-Za-z]{2,})\b')
SAFE_DOMAINS = {b'example.com', b'example.test', b'example.invalid', b'users.noreply.github.com'}
SENSITIVE_PATH = re.compile(r'(^|/)(?:auth[^/]*\.json|\.env(?:\..*)?|status\.json|.*\.(?:pem|key|p12|pfx|sqlite.*|db|log|keychain.*))$')


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT)


def inspect(name, data):
    issues = []
    if SENSITIVE_PATH.search(name) or any(part in ('.codex', '.state', 'dist', '.build-cache') for part in pathlib.PurePosixPath(name).parts):
        issues.append('private/generated file tracked')
    # Screenshot pixels are checked visually; do not pretend regex can audit images.
    if (name.endswith('.png') and data.startswith(b'\x89PNG\r\n\x1a\n')) or (name.endswith('.jpg') and data.startswith(b'\xff\xd8\xff')):
        return issues
    if b'\x00' in data:
        issues.append('unexpected binary; review before publication')
        return issues
    for label, pattern in PATTERNS.items():
        if re.search(pattern, data):
            issues.append(label)
    if any(m.group(1).lower() not in SAFE_DOMAINS for m in EMAIL.finditer(data)):
        issues.append('non-example email address')
    return issues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', action='store_true', help='also inspect every reachable historical blob')
    args = parser.parse_args()
    failures, count = [], 0
    for name in git('ls-files', '-z').decode().split('\0'):
        if not name:
            continue
        path = ROOT/name
        count += 1
        if path.is_symlink():
            failures.append((name, ['tracked symlink']))
        elif path.exists():
            issues = inspect(name, path.read_bytes())
            if issues:
                failures.append((name, issues))
    if not count:
        raise SystemExit('No tracked files. Stage the intended public files first.')
    blobs = 0
    if args.history and subprocess.run(['git', 'rev-parse', '--verify', 'HEAD'], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        for row in git('rev-list', '--objects', '--all').decode().splitlines():
            oid, separator, name = row.partition(' ')
            if not separator or git('cat-file', '-t', oid).strip() != b'blob':
                continue
            blobs += 1
            issues = inspect(name, git('cat-file', 'blob', oid))
            if issues:
                failures.append(('history:'+name, issues))
        metadata = git('log', '--all', '--format=%an <%ae> | %cn <%ce>%n%B')
        issues = inspect('commit metadata', metadata)
        if issues:
            failures.append(('commit metadata', issues))
    for name, issues in failures:
        print(f'{name}: {", ".join(issues)}', file=sys.stderr)
    print(f'Checked {count} tracked files and {blobs} historical blobs; {len(failures)} findings.')
    print('Images and uncommon secret formats still require manual review.')
    return bool(failures)


if __name__ == '__main__':
    raise SystemExit(main())
