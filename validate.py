import json, re, shutil, tempfile, xml.etree.ElementTree as ET
from pathlib import Path
from config import ROOT, load_config
import build

def check():
    cfg=load_config(); payload=json.loads((ROOT/'data/offers.json').read_text(encoding='utf-8'))
    assert {p['id'] for p in cfg['providers']} <= {r['provider'] for r in cfg['extractors']}, 'Every configured provider needs an extraction or availability rule'
    assert {p['id'] for p in cfg['providers']} == set(payload.get('source_status', {})), 'Every configured provider needs a recorded source status'
    assert all(o.get('price') != 0 for o in payload.get('offers', [])), 'A zero price must be represented as source text, not a monthly price'
    build.build()
    files=list((ROOT/'site').rglob('*.html'))
    assert files, 'No HTML was built'
    for f in files:
        text=f.read_text(encoding='utf-8')
        assert '$' not in re.sub(r'\$[0-9.,]+','',text), f'Unfilled template in {f}'
        assert '<script type="application/ld+json">' in text, f'Missing JSON-LD in {f}'
        for raw in re.findall(r'<script type="application/ld\+json">(.*?)</script>',text): json.loads(raw)
    assert (ROOT/'site/robots.txt').exists() and (ROOT/'site/sitemap.xml').exists()
    robots=(ROOT/'site/robots.txt').read_text(encoding='utf-8')
    assert 'Sitemap: '+cfg['site']['domain'].rstrip('/')+'/sitemap.xml' in robots, 'robots.txt must advertise the canonical sitemap'
    sitemap=ET.parse(ROOT/'site/sitemap.xml').getroot()
    ns={'sm':'http://www.sitemaps.org/schemas/sitemap/0.9'}
    entries=sitemap.findall('sm:url',ns)
    assert entries, 'Sitemap has no URLs'
    for entry in entries:
        loc=entry.findtext('sm:loc',default='',namespaces=ns)
        lastmod=entry.findtext('sm:lastmod',default='',namespaces=ns)
        assert loc.startswith(cfg['site']['domain'].rstrip('/')+'/'), f'Non-canonical sitemap URL: {loc}'
        assert lastmod.endswith('Z'), f'Missing capture timestamp: {loc}'
        route=loc.removeprefix(cfg['site']['domain'].rstrip('/')).strip('/')
        output=ROOT/'site'/route/'index.html' if route else ROOT/'site/index.html'
        assert output.exists(), f'Sitemap URL has no generated page: {loc}'
    altered=Path(tempfile.mkdtemp())/'site.ilang'; altered.write_text((ROOT/'.ilang/site.ilang').read_text(encoding='utf-8').replace('Hostinger |','Hostinger Proof |',1),encoding='utf-8')
    trial=Path(tempfile.mkdtemp())/'site'; build.build(altered,trial)
    assert 'Hostinger Proof' in (trial/'index.html').read_text(encoding='utf-8'), 'site.ilang change did not affect build'
    shutil.rmtree(altered.parent,ignore_errors=True); shutil.rmtree(trial.parent,ignore_errors=True)
    print(f'Validated {len(files)} HTML pages and configuration-driven rebuild.')
if __name__=='__main__': check()
