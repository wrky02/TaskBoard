"""Write reproducible build identifiers and redistribute runtime license files."""
import argparse
from importlib import metadata
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('output',type=Path)
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    out=args.output.resolve();licenses=out/'licenses';licenses.mkdir(parents=True,exist_ok=True)
    components=[]
    for name in ['openpyxl','et_xmlfile','sqlcipher3','cryptography','cffi','pycparser']:
        package=metadata.distribution(name)
        entries=[]
        for item in package.files or []:
            if not any(token in item.name.lower() for token in ['license','licence','copying']):continue
            source=Path(package.locate_file(item))
            if not source.is_file():continue
            dest=licenses/name/item.name;dest.parent.mkdir(exist_ok=True)
            shutil.copyfile(source,dest);entries.append(dest.relative_to(out).as_posix())
        if not entries:raise RuntimeError(f'Missing license for {name}')
        components.append(dict(name=name,version=package.version,license_files=entries))
    python_license=Path(sys.base_prefix)/'LICENSE.txt'
    if not python_license.is_file():python_license=Path(sys.base_prefix)/'lib'/f'python{sys.version_info.major}.{sys.version_info.minor}'/'LICENSE.txt'
    if not python_license.is_file():raise RuntimeError('Python runtime license not found')
    shutil.copyfile(python_license,licenses/'PYTHON-LICENSE.txt')
    dotnet=Path(shutil.which('dotnet') or '')
    if not dotnet.is_file():raise RuntimeError('dotnet was not found')
    dotnet=dotnet.resolve().parent
    for filename in ['LICENSE.txt','ThirdPartyNotices.txt']:
        source=dotnet/filename
        if not source.is_file():raise RuntimeError(f'.NET {filename} not found')
        shutil.copyfile(source,licenses/('DOTNET-'+filename))
    for source in (root/'packaging'/'licenses').glob('*'):
        if source.is_file():shutil.copyfile(source,licenses/source.name)
    version=ET.parse(root/'native/TaskBoard/TaskBoard.csproj').find('.//Version').text
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
    sdk=subprocess.check_output(['dotnet','--version'],cwd=root,text=True).strip()
    report=dict(application='TaskBoard',version=version,source_commit=commit,
                dotnet_sdk=sdk,dotnet_target='net10.0-windows',python=platform.python_version(),components=components)
    (out/'BUILD-INFO.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'Build identifiers and licenses recorded for TaskBoard {version} at {commit}')


if __name__=='__main__':main()
