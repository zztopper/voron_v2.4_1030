#!/home/pi/klippy-env/bin/python
"""Generate graphs from latest completed Klipper resonance measurements."""
import json,os,sys,shutil,subprocess,urllib.request
from pathlib import Path
from datetime import datetime
ROOT=Path('/home/pi/printer_data/config/input_shaper')
PYTHON='/home/pi/klippy-env/bin/python'
SCRIPTS=Path('/home/pi/klipper/scripts')
def latest(pattern):
 files=list(Path('/tmp').glob(pattern))
 if not files:raise RuntimeError('No completed measurement: '+pattern)
 return max(files,key=lambda p:p.stat().st_mtime_ns)
def run(args,dest):
 env=dict(os.environ,MPLBACKEND='Agg')
 r=subprocess.run([PYTHON]+args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,env=env,check=False)
 (dest/'analysis.txt').open('a').write(r.stdout)
 print(r.stdout,flush=True)
 if r.returncode:raise RuntimeError('Graph generation failed')
def main(mode):
 if mode=='SHAPER':
  sources=[latest('resonances_x_*.csv'),latest('resonances_y_*.csv')]
 elif mode=='BELT':sources=[latest('raw_data_axis*b*.csv'),latest('raw_data_axis*a*.csv')]
 else:raise RuntimeError('Expected SHAPER or BELT')
 if abs(sources[0].stat().st_mtime-sources[1].stat().st_mtime)>900:raise RuntimeError('Measurements are from different sessions')
 dest=ROOT/(datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+mode.lower());dest.mkdir(parents=True)
 copies=[]
 for src in sources:
  copy=dest/src.name;shutil.copy2(src,copy);copies.append(copy)
 if mode=='SHAPER':
  status=json.load(urllib.request.urlopen('http://localhost:7125/printer/objects/query?toolhead',timeout=5))
  scv=status['result']['status']['toolhead']['square_corner_velocity']
  for axis,src in zip('xy',copies):
   run([str(SCRIPTS/'calibrate_shaper.py'),str(src),'--scv',str(scv),'-o',str(dest/(axis+'.png'))],dest)
 else:run([str(SCRIPTS/'graph_accelerometer.py'),'-c']+[str(p) for p in copies]+['-o',str(dest/'belts.png')],dest)
 print('Results: '+str(dest),flush=True)
if __name__=='__main__':main(sys.argv[1].upper())
