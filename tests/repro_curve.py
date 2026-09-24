"""Compare same-text render with the saved Photoshop pixels of a real PSD.
Usage: python3 studio/tests/repro_curve.py /path/to/sample.psd --out /tmp/curve
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from psd_tools import PSDImage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import magnet


def compare(psd_path, out):
    out.mkdir(parents=True, exist_ok=True)
    analysis = magnet.analyze(psd_path)
    psd = PSDImage.open(psd_path)
    field = next(f for f in analysis['fields'] if f['arc'])
    layer = next(l for l in psd.descendants() if l.name == field['layer'])
    tdir = out / 'template'
    (tdir / 'fonts').mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy2(psd_path.parent / field['font_file'], tdir / 'fonts' / field['font_file'])
    rendered = Image.new('RGBA', psd.size)
    magnet._draw_field(rendered, field, tdir, field['text'])
    original = Image.new('RGBA', psd.size)
    original.paste(layer.topil().convert('RGBA'), layer.offset)
    x0, y0, x1, y1 = layer.bbox
    roi = (max(0,x0-300),max(0,y0-350),min(psd.width,x1+300),min(psd.height,y1+450))
    a, b = original.crop(roi), rendered.crop(roi)
    color = np.array(field['color'][:3])
    def mask(im):
        arr = np.asarray(im).astype(int)
        return (arr[...,3]>127) & (np.abs(arr[...,:3]-color).max(2)<20)
    ma, mb = mask(a), mask(b)
    iou = float((ma & mb).sum()/max(1,(ma | mb).sum()))
    def bbox(m):
        yy,xx=np.where(m)
        return [int(xx.min()),int(yy.min()),int(xx.max()+1),int(yy.max()+1)] if len(xx) else None
    result={'text':field['text'],'iou':round(iou,4),'original_bbox':bbox(ma),'rendered_bbox':bbox(mb),'field':field}
    (out/'metrics.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    previews=[]
    for title, im in [('PSD original',a),('Studio render',b)]:
        im.thumbnail((1100,500))
        panel=Image.new('RGB',(1100,im.height+40),'#eee5d8')
        panel.paste(im,(0,40),im)
        ImageDraw.Draw(panel).text((12,10),title,fill='black')
        previews.append(panel)
    sheet=Image.new('RGB',(1100,sum(p.height for p in previews)), 'white')
    y=0
    for p in previews: sheet.paste(p,(0,y)); y+=p.height
    sheet.save(out/'comparison.png')
    print(json.dumps({k:v for k,v in result.items() if k!='field'},ensure_ascii=False))
    assert iou >= 0.85, f'Curved text differs from PSD: fill IoU={iou:.3f} < 0.85'


if __name__=='__main__':
    ap=argparse.ArgumentParser(); ap.add_argument('psd',type=Path); ap.add_argument('--out',type=Path,required=True)
    args=ap.parse_args(); compare(args.psd,args.out)
