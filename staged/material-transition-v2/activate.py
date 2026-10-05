"""Manual v2 activation. There is no scheduled/automatic activation."""
import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT=Path('/home/pi/printer_data/config')
BASE='http://localhost:7125'
FILES={'afc_material_transition.py':'script/afc_material_transition.py',
       'material_transition.cfg':'material_transition.cfg',
       'profiles.json':'material_transition_profiles.json',
       'guarded_purge.cfg':'material_transition_purge.cfg'}


def query():
    url=BASE+'/printer/objects/query?webhooks&print_stats&pause_resume&virtual_sdcard&gcode_macro%20PRINT_START'
    return json.load(urllib.request.urlopen(url,timeout=5))['result']['status']


def require_idle(status,allow_error=False):
    if allow_error and status['webhooks']['state'] in ('error','shutdown'):
        return
    if status['webhooks']['state']!='ready':
        raise RuntimeError('Klipper is not ready')
    if (status['print_stats']['state'] in ('printing','paused')
            or status['pause_resume']['is_paused'] or status['virtual_sdcard']['is_active']
            or status['gcode_macro PRINT_START']['state']!='Prepare'):
        raise RuntimeError('Printing/startup is active. No files or printer state were changed.')


def atomic_copy(src,dst):
    dst.parent.mkdir(parents=True,exist_ok=True)
    temp=dst.with_name(dst.name+'.v2-new')
    shutil.copy2(src,temp)
    owner=dst.stat() if dst.exists() else dst.parent.stat()
    os.chown(temp,owner.st_uid,owner.st_gid)
    temp.replace(dst)


def restart_without_init():
    p=ROOT/'printer.cfg';original=p.read_text()
    needle='[delayed_gcode INIT]\n  initial_duration: 2'
    if original.count(needle)!=1:
        raise RuntimeError('INIT startup section changed; review before restarting')
    p.write_text(original.replace(needle,'[delayed_gcode INIT]\n  initial_duration: 0',1))
    try:
        subprocess.run(['systemctl', 'restart', 'klipper'], check=True, timeout=30)
        for _ in range(40):
            time.sleep(1)
            try:
                state=query()['webhooks']
            except Exception:
                continue
            if state['state']=='ready':return
            if state['state'] in ('error','shutdown'):raise RuntimeError(state['state_message'])
        raise RuntimeError('Klipper restart timed out')
    finally:
        p.write_text(original)


def activate(package, restart=False):
    require_idle(query())  # Must precede mkdir, backups and all writes.
    p=ROOT/'printer.cfg'
    if p.read_text().count('[include material_transition.cfg]')!=1:
        raise RuntimeError('Expected existing v1 include; review printer.cfg')
    extra=Path('/home/pi/klipper/klippy/extras/afc_material_transition.py')
    if not extra.is_symlink() or extra.resolve()!=(ROOT/'script/afc_material_transition.py').resolve():
        raise RuntimeError('Unexpected Klipper extension path')
    for src in FILES:
        if not (package/src).is_file():raise RuntimeError('Missing staged file '+src)
    backup=ROOT/'config_backups'/('material-transition-v2-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    backup.mkdir()
    manifest={}
    for dest in FILES.values():
        target=ROOT/dest;manifest[dest]=target.exists()
        if target.exists():atomic_copy(target,backup/dest)
    shutil.copy2(p,backup/'printer.cfg')
    shutil.copy2(ROOT/'fan.cfg',backup/'fan.cfg')
    shutil.copy2('/home/pi/klipper_config/.variables.stb',backup/'variables.stb')
    (backup/'manifest.json').write_text(json.dumps(manifest,indent=2))
    require_idle(query())
    for src,dest in FILES.items():atomic_copy(package/src,ROOT/dest)
    print('Installed v2 files. Backup:',backup,flush=True)
    if restart:
        require_idle(query())
        restart_without_init()
        data=json.load(urllib.request.urlopen(BASE+'/printer/objects/query?afc_material_transition',timeout=5))
        version=data['result']['status']['afc_material_transition']['version']
        if version!='2.0.0':raise RuntimeError('Unexpected loaded module version: '+version)
        print('Klipper ready with v2. Confirm current material after physical inspection.',flush=True)
    else:
        print('Restart required. No restart performed.',flush=True)


def rollback(backup,restart=False):
    require_idle(query(),allow_error=True)
    manifest=json.loads((backup/'manifest.json').read_text())
    for dest,existed in manifest.items():
        if dest not in FILES.values():raise RuntimeError('Unexpected rollback destination')
        if existed:atomic_copy(backup/dest,ROOT/dest)
        else:(ROOT/dest).unlink(missing_ok=True)
    # Do not restore unrelated printer/fan settings or historical variables.
    if restart:restart_without_init()
    print('Restored module files from',backup,flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--restart',action='store_true')
    parser.add_argument('--rollback',type=Path)
    args=parser.parse_args()
    try:
        if args.rollback:rollback(args.rollback,args.restart)
        else:activate(Path(__file__).resolve().parent,args.restart)
    except Exception as exc:
        print('Activation refused/failed:',exc,file=sys.stderr)
        sys.exit(1)
