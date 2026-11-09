"""Build a local desktop app and generate a per-user launchd configuration."""
import argparse
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys


LABEL='local.personal-assistant.supervisor'


def launch_agent(root):
    root=Path(root).resolve()
    return dict(Label=LABEL,ProgramArguments=[str(root/'.venv/bin/python'),str(root/'supervisor.py')],
                WorkingDirectory=str(root),RunAtLoad=True,KeepAlive=dict(SuccessfulExit=False),
                ThrottleInterval=30,ProcessType='Background',
                StandardOutPath=str(root/'logs/launchd.log'),StandardErrorPath=str(root/'logs/launchd.log'),
                EnvironmentVariables=dict(PATH='/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin',PYTHONUNBUFFERED='1'))


def build(root,output,port=5002):
    root,output=Path(root).resolve(),Path(output).resolve()
    if not 1<=port<=65535:raise ValueError('Invalid port')
    if output.exists():raise ValueError('App already exists; choose another output path')
    if not (root/'.venv/bin/python').is_file():raise ValueError('Project virtualenv is missing')
    contents=output/'Contents';binary=contents/'MacOS'/'Assistant'
    binary.parent.mkdir(parents=True)
    try:
        subprocess.run(['xcrun','swiftc','-O','-module-cache-path','/tmp/assistant-swift-module-cache',
                        str(root/'desktop/Assistant.swift'),'-o',str(binary),'-framework','Cocoa','-framework','WebKit'],check=True,timeout=180)
        info=dict(CFBundleIdentifier='local.personal-assistant.desktop',CFBundleName='ИИ Ассистент',
                  CFBundleExecutable='Assistant',CFBundlePackageType='APPL',CFBundleShortVersionString='0.1.0',
                  NSHighResolutionCapable=True,LSMinimumSystemVersion='12.0',
                  AssistantProjectRoot=str(root),AssistantPanelURL=f'http://127.0.0.1:{port}',
                  NSAppTransportSecurity=dict(NSAllowsLocalNetworking=True))
        with (contents/'Info.plist').open('wb') as stream:plistlib.dump(info,stream)
        subprocess.run(['codesign','--force','--deep','--sign','-',str(output)],check=True,timeout=30,capture_output=True)
    except BaseException:
        shutil.rmtree(output,ignore_errors=True)
        raise
    return output


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('build','build-ocr','plist','install','uninstall'))
    parser.add_argument('--root',default=str(Path(__file__).resolve().parent))
    parser.add_argument('--output')
    parser.add_argument('--port',type=int,default=5002)
    args=parser.parse_args();root=Path(args.root).resolve()
    if sys.platform!='darwin':parser.error('macOS is required')
    if args.action=='build-ocr':
        output=Path(args.output) if args.output else root/'dist/document-ocr'
        output.parent.mkdir(parents=True,exist_ok=True)
        subprocess.run(['xcrun','swiftc','-O','-module-cache-path','/tmp/assistant-swift-module-cache',str(root/'desktop/DocumentOCR.swift'),'-o',str(output),'-framework','AppKit','-framework','PDFKit','-framework','Vision'],check=True,timeout=180)
        print(output);return
    if args.action=='build':
        print(build(root,args.output or str(root/'dist/Assistant.app'),args.port));return
    document=launch_agent(root)
    target=Path(args.output) if args.output else root/'dist'/f'{LABEL}.plist'
    if args.action=='plist':
        target.parent.mkdir(parents=True,exist_ok=True)
        with target.open('wb') as stream:plistlib.dump(document,stream)
        target.chmod(0o600);print(target);return
    target=Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist'
    domain=f'gui/{os.getuid()}'
    if args.action=='uninstall':
        subprocess.run(['launchctl','bootout',domain+'/'+LABEL],check=False)
        target.unlink(missing_ok=True);return
    if target.exists():parser.error('LaunchAgent already exists; uninstall it before replacing')
    if not (root/'.venv/bin/python').is_file():parser.error('Project virtualenv is missing')
    (root/'logs').mkdir(exist_ok=True,mode=0o700)
    target.parent.mkdir(parents=True,exist_ok=True)
    with target.open('xb') as stream:plistlib.dump(document,stream)
    target.chmod(0o600)
    try:subprocess.run(['launchctl','bootstrap',domain,str(target)],check=True,timeout=30)
    except BaseException:
        target.unlink(missing_ok=True);raise
    print(target)


if __name__=='__main__':main()
