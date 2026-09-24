"""Compare same-text render with the saved Photoshop pixels of a real PSD.
Usage: python3 studio/tests/repro_curve.py /path/to/sample.psd --out /tmp/curve
"""
import argparse
import copy
import shutil
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from psd_tools import PSDImage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import magnet


def compare(psd_path, out, integration=False):
    out.mkdir(parents=True, exist_ok=True)
    analysis = magnet.analyze(psd_path)
    psd = PSDImage.open(psd_path)
    field = next(f for f in analysis['fields'] if f['arc'])
    layer = next(l for l in psd.descendants() if l.name == field['layer'])
    tdir = out / 'template'
    (tdir / 'fonts').mkdir(parents=True, exist_ok=True)
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
    geometry = field['text_geometry']
    assert geometry['warp']['style'] == 'warpArch'
    assert not magnet._supports_arch({**geometry, 'warp': {**geometry['warp'], 'perspective': 10}})
    # Templates are JSON: round-trip the metadata before testing replacement names.
    loaded = json.loads(json.dumps(field))
    roundtrip = Image.new('RGBA', psd.size)
    magnet._draw_field(roundtrip, loaded, tdir, field['text'])
    assert roundtrip.tobytes() == rendered.tobytes(), 'JSON lost warp geometry'
    # Moving a box should move the result, not change the warp or glyph size.
    moved = copy.deepcopy(field)
    moved['box'] = [v + (40 if i % 2 == 0 else 60) for i, v in enumerate(field['box'])]
    shifted = Image.new('RGBA', psd.size)
    magnet._draw_field(shifted, moved, tdir, field['text'])
    old_box, new_box = rendered.getbbox(), shifted.getbbox()
    assert max(abs(new_box[i] - old_box[i] - (40 if i % 2 == 0 else 60)) for i in range(4)) <= 1
    for name in ['Amy', 'Mrs. Alexandra Montgomery Wilson']:
        replacement = Image.new('RGBA', psd.size)
        magnet._draw_field(replacement, loaded, tdir, name)
        bb = replacement.getbbox()
        assert bb and bb[0] >= field['box'][0] - 50 and bb[2] <= field['box'][2] + 50, (name, bb)
        assert replacement.tobytes() != rendered.tobytes(), 'Replacement text was ignored'
    for sag in [0, -field['arc']]:
        edited = {**loaded, 'arc': sag}
        alternative = Image.new('RGBA', psd.size)
        magnet._draw_field(alternative, edited, tdir, 'Wilson', '#ff0000')
        arr = np.asarray(alternative)
        red = (arr[...,0] > 230) & (arr[...,1] < 20) & (arr[...,3] > 200)
        assert red.sum() > 1000, 'Edited bend or color override failed'
    # Existing templates without geometry must continue to render.
    legacy = dict(field); legacy.pop('text_geometry')
    old_render = Image.new('RGBA', psd.size)
    magnet._draw_field(old_render, legacy, tdir, 'Wilson')
    assert old_render.getbbox(), 'Legacy template failed'
    print('PASS: metadata, replacement names, move, zero/negative bend, color, legacy template')
    if integration:
        tpl = magnet.save_template(psd_path, 'VTY162607A01_back_arch', [field], out / 'data')
        reloaded = magnet.load_template(tpl['slug'], out / 'data')
        assert reloaded['fields'][0]['text_geometry'] == geometry
        rows = [{'order': 'original-name', field['key']: field['text']},
                {'order': 'new-name', field['key']: 'Mrs. Anderson'}]
        paths = magnet.render_rows(tpl['slug'], rows, out / 'data', out / 'renders', ['png', 'jpg'])
        assert len(paths) == 2
        for path in paths:
            for ext in ['.png', '.jpg']:
                with Image.open(path.with_suffix(ext)) as im:
                    assert im.size == psd.size
        # Full PSD workflow must still put the same warped text at the measured location.
        full = Image.open(paths[0]).convert('RGBA').crop(roi)
        fm = mask(full)
        # Other fixed artwork (the number below the name) is outside this field.
        yy, xx = np.where(ma)
        window = np.zeros_like(ma)
        window[max(0, yy.min()-20):yy.max()+21, max(0, xx.min()-20):xx.max()+21] = True
        fm &= window
        full_iou = float((ma & fm).sum() / max(1, (ma | fm).sum()))
        assert full_iou >= .85, f'Full template workflow changed placement: {full_iou:.3f}'
        reference = psd.composite().convert('RGB')
        reference.save(out / 'original-psd.png')
        # A fixed layer must not be re-rendered with altered/clipped Photoshop effects.
        for layer in psd.descendants():
            if layer.kind == 'type' and layer.name != field['layer']:
                ref = np.asarray(reference.crop(layer.bbox)).astype(int)
                actual = np.asarray(Image.open(paths[0]).convert('RGB').crop(layer.bbox)).astype(int)
                error = float(np.abs(ref - actual).mean())
                assert error < 0.1, f'Fixed layer {layer.name} changed: mean pixel error={error:.3f}'
        panels = []
        for label, im in [('PSD original', reference), ('Studio: same name', Image.open(paths[0])),
                          ('Studio: new name', Image.open(paths[1]))]:
            im = im.convert('RGB'); im.thumbnail((600, 800))
            panel = Image.new('RGB', (600, im.height + 35), 'white')
            panel.paste(im, (0, 35)); ImageDraw.Draw(panel).text((10, 10), label, fill='black')
            panels.append(panel)
        sheet = Image.new('RGB', (1800, max(p.height for p in panels)), 'white')
        for i, panel in enumerate(panels): sheet.paste(panel, (i * 600, 0))
        sheet.save(out / 'full-comparison.jpg', quality=95)
        print(f'PASS: save/load template + PNG/JPG batch; full-workflow IoU={full_iou:.4f}')


if __name__=='__main__':
    ap=argparse.ArgumentParser(); ap.add_argument('psd',type=Path); ap.add_argument('--out',type=Path,required=True)
    ap.add_argument('--integration', action='store_true')
    args=ap.parse_args(); compare(args.psd,args.out,args.integration)
