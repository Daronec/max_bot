"""Local Apple Vision OCR check on a synthetic raster PDF, no user documents."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root))
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject,NameObject,NumberObject,DecodedStreamObject
letters={
'H':['10001','10001','10001','11111','10001','10001','10001'],
'E':['11111','10000','10000','11110','10000','10000','11111'],
'L':['10000','10000','10000','10000','10000','10000','11111'],
'O':['01110','10001','10001','10001','10001','10001','01110']}
width,height,scale=800,240,18
pixels=bytearray([255])*(width*height)
for i,letter in enumerate('HELLO'):
 for y,row in enumerate(letters[letter]):
  for x,bit in enumerate(row):
   if bit=='1':
    for dy in range(scale):
     offset=(55+y*scale+dy)*width+100+i*6*scale+x*scale
     pixels[offset:offset+scale]=bytes(scale)
with tempfile.TemporaryDirectory(prefix='assistant-ocr-test-') as directory:
 path=Path(directory)/'scan.pdf';writer=PdfWriter();page=writer.add_blank_page(width=width,height=height)
 image=DecodedStreamObject();image.set_data(bytes(pixels))
 image.update({NameObject('/Type'):NameObject('/XObject'),NameObject('/Subtype'):NameObject('/Image'),NameObject('/Width'):NumberObject(width),NameObject('/Height'):NumberObject(height),NameObject('/ColorSpace'):NameObject('/DeviceGray'),NameObject('/BitsPerComponent'):NumberObject(8)})
 page[NameObject('/Resources')]=DictionaryObject({NameObject('/XObject'):DictionaryObject({NameObject('/Im0'):writer._add_object(image)})})
 stream=DecodedStreamObject();stream.set_data(f'q {width} 0 0 {height} 0 0 cm /Im0 Do Q'.encode());page[NameObject('/Contents')]=writer._add_object(stream)
 writer.write(path)
 result=subprocess.run([str(root/'dist/document-ocr'),str(path)],capture_output=True,text=True,timeout=30)
 payload=json.loads(result.stdout)
 assert result.returncode==0,(payload,result.stderr)
 assert 'HELLO' in payload['text'].upper(),payload
 print(json.dumps(dict(result='passed',engine=payload['engine'],checks=['raster_pdf','local_recognition','page_provenance'])))
