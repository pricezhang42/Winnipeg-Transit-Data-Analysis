"""Shared normalization, coordinate and source-fingerprint helpers for history builds."""
import hashlib
import math
import re


def destination(value):
    return re.sub(r'^to\s+', '', (value or '').strip().lower())

def point(value):
    m = re.fullmatch(r'POINT\s*\(\s*([-+\d.eE]+)\s+([-+\d.eE]+)\s*\)', value or '')
    if not m: return None
    lon, lat = map(float, m.groups())
    return (lon, lat) if math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90 else None

def distance(a,b):
    lon1,lat1,lon2,lat2=map(math.radians,(*a,*b))
    v=math.sin((lat2-lat1)/2)**2+math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 12742000*math.asin(min(1,math.sqrt(v)))

def digest(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()
