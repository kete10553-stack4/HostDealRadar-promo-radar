"""Conservative official-source checker for HostDealRadar."""
import argparse, json, re
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from config import ROOT, load_config, slug

DATA = ROOT / 'data/offers.json'

def now(): return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')

def fetch(url, cfg):
    request = Request(url, headers={'User-Agent': cfg['settings']['user_agent'], 'Accept': 'text/html,application/xhtml+xml,text/plain'})
    with urlopen(request, timeout=cfg['settings']['timeout_seconds']) as response:
        body = response.read(cfg['settings']['max_response_bytes'] + 1)
        if len(body) > cfg['settings']['max_response_bytes']:
            raise ValueError('response exceeds configured limit')
        return response.status, body.decode(response.headers.get_content_charset() or 'utf-8', 'replace')

def permitted(source_url, cfg):
    parts = urlsplit(source_url); robots = f'{parts.scheme}://{parts.netloc}/robots.txt'
    try: status, text = fetch(robots, cfg)
    except (HTTPError, URLError, ValueError) as exc: return False, f'robots check failed: {exc}'
    if status != 200: return False, f'robots returned HTTP {status}'
    path = parts.path or '/'; groups=[]; agents=[]; directives=[]
    for raw in text.splitlines() + ['']:
        line = raw.split('#', 1)[0].strip()
        if not line:
            if agents: groups.append((agents, directives)); agents=[]; directives=[]
            continue
        if ':' not in line: continue
        key, value = [x.strip() for x in line.split(':',1)]
        if key.lower() == 'user-agent':
            if directives: groups.append((agents, directives)); agents=[]; directives=[]
            agents.append(value.lower())
        elif key.lower() in ('allow','disallow') and agents: directives.append((key.lower(), value))
    matched=[]
    for group_agents, group_directives in groups:
        if '*' in group_agents or 'hostdealradar' in group_agents: matched.extend(group_directives)
    rules=[]
    for kind, value in matched:
        if value and path.startswith(value): rules.append((len(value), kind))
    if not rules: return True, 'allowed by robots.txt'
    longest=max(x[0] for x in rules); kinds=[kind for length,kind in rules if length == longest]
    return ('allow' in kinds), ('allowed' if 'allow' in kinds else 'disallowed') + ' by robots.txt'

def clean(text):
    return re.sub(r'\s+', ' ', re.sub(r'(?is)<(script|style).*?</\1>', ' ', text))

def run(config_path=None):
    cfg=load_config(config_path); offers=[]; statuses={}; extractors=cfg['extractors']
    for provider in cfg['providers']:
        ok, message=permitted(provider['source_url'], cfg)
        if not ok: statuses[provider['id']]={'status':'unavailable','reason':message}; continue
        try: status, page=fetch(provider['source_url'], cfg)
        except HTTPError as exc: statuses[provider['id']]={'status':'unavailable','reason':f'Official source returned HTTP {exc.code}.'}; continue
        except (URLError, ValueError) as exc: statuses[provider['id']]={'status':'unavailable','reason':f'Official source could not be checked: {exc}.'}; continue
        if status != 200 or re.search(r'(?i)(captcha|cf-chl|access denied|verify you are human)', page):
            statuses[provider['id']]={'status':'unavailable','reason':'Official source was unavailable or presented a challenge.'}; continue
        statuses[provider['id']]={'status':'checked','reason':'Official source responded successfully.'}
        visible=clean(page)
        for rule in [r for r in extractors if r.get('provider') == provider['id']]:
            match=re.search(rule['pattern'], visible)
            if not match: continue
            values=match.groupdict(); price=values.get('price')
            offer={'slug':slug(provider['id']+'-'+rule['title']), 'provider':provider['id'], 'title':rule['title'], 'category':rule.get('category','Hosting'), 'source_url':provider['source_url'], 'offer_url':provider['affiliate_url'] or provider['source_url'], 'fetched_at':now(), 'evidence':'Matched the deterministic rule recorded in .ilang/site.ilang.'}
            if price: offer['price']=float(price)
            for key in ('discount','months'):
                if values.get(key): offer['discount_percent' if key=='discount' else 'commitment_months']=int(values[key])
            offer['currency']='USD'; offer['billing_period']=rule.get('billing_period','month')
            if rule.get('condition'): offer['condition']=rule['condition']
            offers.append(offer)
    payload={'generated_at':now(), 'offers':offers, 'source_status':statuses}
    DATA.write_text(json.dumps(payload,indent=2)+ '\n', encoding='utf-8')
    return payload

if __name__ == '__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--config'); args=parser.parse_args(); result=run(args.config)
    print(f"Checked {len(result['source_status'])} sources; published {len(result['offers'])} offers.")
