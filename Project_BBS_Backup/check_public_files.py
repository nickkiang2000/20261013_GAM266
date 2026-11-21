"""Read-only review of the explicit public source manifest and staged filenames."""
import argparse
import ipaddress
import json
from pathlib import Path
import re
import subprocess

ROOT=Path(__file__).resolve().parent


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--staged',action='store_true',help='Also check Git staged paths; requires an existing repository.')
    args=parser.parse_args()
    allowed={line.strip() for line in (ROOT/'public-files.txt').read_text(encoding='utf-8').splitlines() if line.strip() and not line.startswith('#')}
    problems=[]
    for name in sorted(allowed):
        path=(ROOT/name).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file():
            problems.append(name+': missing or outside project');continue
        text=path.read_text(encoding='utf-8')
        if re.search(r'[A-Za-z]:[/\\]Users[/\\][A-Za-z0-9_-]+',text):
            problems.append(name+': personal Windows home path')
        for match in re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}\b',text):
            try:ip=ipaddress.ip_address(match)
            except ValueError:continue
            if not ip.is_loopback and not any(ip in ipaddress.ip_network(net) for net in ('192.0.2.0/24','198.51.100.0/24','203.0.113.0/24')):
                problems.append(name+': review embedded IP address')
        if name.startswith('tests/fixtures/'):
            data=json.loads(text)
            if not all(row.get('synthetic') is True for row in data):problems.append(name+': non-synthetic fixture')
    if args.staged:
        result=subprocess.run(['git','diff','--cached','--name-only','--diff-filter=ACMR'],cwd=ROOT,capture_output=True,text=True)
        if result.returncode:problems.append('Git staged check unavailable; inspect repository location.')
        else:
            problems.extend('Not in public manifest: '+name for name in result.stdout.splitlines() if name not in allowed)
    for message in problems:print(message)
    print(f'Reviewed {len(allowed)} public files. This is a limited automated check, not a Git history audit.')
    return bool(problems)


if __name__=='__main__':raise SystemExit(main())
