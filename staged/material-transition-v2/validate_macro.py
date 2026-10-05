"""Render the staged purge with live READ-ONLY variables; send no G-code."""
import configparser
import json
import math
from pathlib import Path
import re
import urllib.parse
import urllib.request
import jinja2
from afc_material_transition import Policy

names=['gcode_macro _AFC_GLOBAL_VARS','gcode_macro _AFC_POOP_VARS','toolhead','gcode_move','fan']
url='http://localhost:7125/printer/objects/query?'+ '&'.join(urllib.parse.quote(n) for n in names)
printer=json.load(urllib.request.urlopen(url,timeout=5))['result']['status']
config=configparser.RawConfigParser(strict=False)
config.read(Path(__file__).with_name('guarded_purge.cfg'))
env=jinja2.Environment('{%','%}','{','}')
template=env.from_string(config['gcode_macro AFC_V2_BLOB']['gcode'])
policy=Policy(json.loads(Path(__file__).with_name('profiles.json').read_text()))
for volume in [12,45,60,180,500]:
    output=template.render(printer=printer,
        params={'PURGE_LENGTH':str(volume/policy.area),'CHUNK_LENGTH':str(12/policy.area)},
        action_respond_info=lambda message:'')
    # Apply the same temporary variable overrides as the adapter, then rerender.
    printer['gcode_macro _AFC_POOP_VARS'].update(purge_length_minimum=.01,purge_cool_time=0,purge_spd=6/policy.area)
    output=template.render(printer=printer,
        params={'PURGE_LENGTH':str(volume/policy.area),'CHUNK_LENGTH':str(12/policy.area)},
        action_respond_info=lambda message:'')
    lines=[line.strip() for line in output.splitlines() if line.strip()]
    total=0.;feed_count=0
    for i,line in enumerate(lines):
        if not line.startswith('G1 '):continue
        e=re.search(r'(?:^|\s)E([-\d.]+)',line)
        if e is None:continue
        length=float(e.group(1));total+=length;feed_count+=1
        assert length*policy.area<=12.00001,line
        assert lines[i+1]=='M400' and lines[i+2]=='AFC_TRANSITION_CHECK'
        f=re.search(r'(?:^|\s)F([-\d.]+)',line)
        assert f and math.isfinite(float(f.group(1))) and float(f.group(1))>0,line
    assert abs(total*policy.area-volume)<.0001,(total*policy.area,volume)
    print('PASS render: %gmm3, %d guarded feed moves; no G-code sent.' % (volume,feed_count))
