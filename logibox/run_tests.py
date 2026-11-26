"""Run legacy test files in isolated processes (some are executable scripts)."""
from pathlib import Path
import argparse
import json
import re
import subprocess
import sys


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output-dir',default=str(Path(__file__).parent/'test-results'))
    args=parser.parse_args()
    root=Path(__file__).resolve().parent
    out=Path(args.output_dir);out.mkdir(parents=True,exist_ok=True)
    results=[]
    for file in sorted(root.glob('test_*.py')):
        result=subprocess.run([sys.executable,str(file)],cwd=root,capture_output=True,text=True,timeout=180)
        log=result.stdout+result.stderr
        (out/(file.stem+'.log')).write_text(log,encoding='utf-8')
        count=re.search(r'Ran (\d+) tests?',log)
        results.append(dict(file=file.name,returncode=result.returncode,unittest_count=int(count[1]) if count else None))
        print(file.name,'PASS' if result.returncode==0 else 'FAIL',flush=True)
    (out/'results.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf-8')
    return int(any(r['returncode'] for r in results))


if __name__=='__main__':raise SystemExit(main())
