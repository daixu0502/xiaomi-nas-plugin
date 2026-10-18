#!/usr/bin/env python3
import binascii, struct, sys, zlib
SIZE = 300
def rounded(x, y, r=58):
    cx=min(max(x,r),SIZE-r); cy=min(max(y,r),SIZE-r)
    return (x-cx)**2+(y-cy)**2 <= r*r
def pixel(x,y):
    if not rounded(x,y): return (0,0,0,0)
    t=(x+y)/(SIZE*2); bg=tuple(round(a*(1-t)+b*t) for a,b in zip((52,211,153),(4,120,87)))
    inside=(63<=x<=237 and 100<=y<=224) or (78<=x<=156 and 72<=y<=117)
    if inside:
        if 139<=x<=161 and 135<=y<=190: return (*bg,255)
        if 117<=x<=183 and 174<=y<=200 and y>=174+abs(x-150)*.55: return (*bg,255)
        return (255,255,255,255)
    return (*bg,255)
def chunk(k,d): return struct.pack('>I',len(d))+k+d+struct.pack('>I',binascii.crc32(k+d)&0xffffffff)
raw=bytearray()
for y in range(SIZE):
    raw.append(0)
    for x in range(SIZE): raw.extend(pixel(x+.5,y+.5))
png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',SIZE,SIZE,8,6,0,0,0))+chunk(b'IDAT',zlib.compress(bytes(raw),9))+chunk(b'IEND',b'')
with open(sys.argv[1],'wb') as f: f.write(png)
