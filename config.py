# ::ILANG [TYPE:module][PROJECT:HostDealRadar]
# ::ROLE{读取site.ilang，提供唯一配置}
# ::BOUNDARY{never:执行配置代码或硬编码厂商清单}
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent

def slug(value):
    return re.sub(r'[^a-z0-9]+', '-', value.lower()).strip('-')

def safe_url(value):
    parts = urlsplit(value)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
        raise ValueError('Only public HTTPS URLs are allowed in configuration')
    return value

def load_config(path=None):
    path = Path(path) if path else ROOT / '.ilang/site.ilang'
    raw = path.read_text(encoding='utf-8')
    if raw.splitlines()[0] != '::ILANG':
        raise ValueError('Missing I-Lang header')
    state = re.search(r'^::STATE\{@SITE, (.+)\}$', raw, re.M)
    if not state:
        raise ValueError('Missing @SITE configuration')
    site = dict(item.strip().split(':', 1) for item in state.group(1).split(','))
    sections = {}
    active = None
    for line in raw.splitlines():
        match = re.match(r'^::MODULE\{([^|}]+)', line)
        if match:
            active = match.group(1)
            sections[active] = []
        elif line.startswith('::'):
            active = None
        elif active and line.strip():
            sections[active].append(line.strip())
    providers = []
    for line in sections['PROVIDERS']:
        fields = [part.strip() for part in line.split('|')]
        if len(fields) != 4:
            raise ValueError('Provider rows require name | website | source | affiliate')
        name, website, source, affiliate = fields
        providers.append(dict(id=slug(name), name=name, website=safe_url(website), source_url=safe_url(source), affiliate_url=safe_url(affiliate) if affiliate else ''))
    if len({p['id'] for p in providers}) != len(providers):
        raise ValueError('Duplicate provider identifiers')
    settings = json.loads('\n'.join(sections['SETTINGS']))
    rules = [json.loads(line) for line in sections.get('EXTRACTORS', [])]
    notes = json.loads('\n'.join(sections.get('PROVIDER_NOTES', ['{}'])))
    browser_observations = json.loads('\n'.join(sections.get('BROWSER_OBSERVATIONS', ['{}'])))
    page_focus = json.loads('\n'.join(sections.get('PAGE_FOCUS', ['{}'])))
    return dict(site=site, settings=settings, providers=providers, extractors=rules,
                fields=sections['FIELDS'][0].split(), notes=notes, browser_observations=browser_observations, page_focus=page_focus,
                excluded=sections.get('EXCLUDED', []), references=sections.get('REFERENCE_SOURCES', []), raw=raw)
