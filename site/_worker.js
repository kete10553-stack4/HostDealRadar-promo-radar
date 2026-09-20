const LINK_HEADER = [
  '</.well-known/api-catalog>; rel="api-catalog"',
  '</openapi.json>; rel="service-desc"',
  '</ai/>; rel="service-doc"',
  '</.well-known/ai-catalog.json>; rel="describedby"'
].join(', ');
function mergedResponse(response, additions = {}) {
  const headers = new Headers(response.headers);
  for (const [name, value] of Object.entries(additions)) headers.set(name, value);
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}
function json(body, status = 200, additions = {}) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store', 'access-control-allow-origin': '*', ...additions } });
}
async function asset(env, request, pathname) {
  const url = new URL(request.url); url.pathname = pathname; url.search = '';
  return env.ASSETS.fetch(new Request(url.toString(), { method: 'GET', headers: request.headers }));
}
function wantsMarkdown(request) { return (request.headers.get('accept') || '').toLowerCase().includes('text/markdown'); }
function unavailable() {
  return json({ error: 'temporarily_unavailable', status: 'under_construction', available: false, capabilities_status: 'planned_contract_only', message: 'Coming soon; authentication is not available.', launch_date: null }, 503, { 'www-authenticate': 'Bearer error="temporarily_unavailable"' });
}
export default {
  async fetch(request, env) {
    const url = new URL(request.url); const path = url.pathname;
    if (path === '/api/agent/lookup') {
      if (request.method !== 'GET') return json({ error: 'method_not_allowed', message: 'Use GET for this read-only endpoint.' }, 405, { allow: 'GET' });
      const provider = (url.searchParams.get('provider') || '').trim().toLowerCase(); const slug = (url.searchParams.get('slug') || '').trim();
      if ((!provider && !slug) || (provider && slug)) return json({ error: 'invalid_query', message: 'Supply exactly one of provider or slug.' }, 400);
      const records = await (await asset(env, request, '/agent-data.json')).json();
      const matches = slug ? records.filter((record) => record.id === slug) : records.filter((record) => record.provider.id === provider || record.provider.name.toLowerCase() === provider);
      if (!matches.length) return json({ status: 'not_found', query: slug ? { slug } : { provider }, records: [], message: 'No public HostDealRadar record matched this exact identifier.' }, 404);
      return json({ status: 'ok', query: slug ? { slug } : { provider }, record_count: Math.min(matches.length, 25), records: matches.slice(0, 25), limitations: 'This read-only API reports published source records. It does not test checkout, availability, eligibility, or provider performance.' });
    }
    if (['/agent-auth/authorize', '/agent-auth/token', '/agent-auth/register', '/agent-auth/claim'].includes(path)) return unavailable();
    const specialAssets = { '/ai/': ['/ai/index.md', 'text/markdown; charset=utf-8'], '/.well-known/api-catalog': ['/.well-known/api-catalog.json', 'application/linkset+json; charset=utf-8'], '/.well-known/oauth-authorization-server': ['/.well-known/oauth-authorization-server', 'application/json; charset=utf-8'], '/.well-known/oauth-protected-resource': ['/.well-known/oauth-protected-resource', 'application/json; charset=utf-8'], '/.well-known/jwks.json': ['/.well-known/jwks.json', 'application/json; charset=utf-8'] };
    if (specialAssets[path]) { const [assetPath, contentType] = specialAssets[path]; return mergedResponse(await asset(env, request, assetPath), { 'content-type': contentType, 'access-control-allow-origin': '*' }); }
    if (path === '/' && wantsMarkdown(request)) return mergedResponse(await asset(env, request, '/ai/index.md'), { 'content-type': 'text/markdown; charset=utf-8', 'vary': 'Accept', 'link': LINK_HEADER });
    const response = await env.ASSETS.fetch(request);
    if (path === '/') return mergedResponse(response, { 'vary': 'Accept', 'link': LINK_HEADER });
    if (path === '/.well-known/ai-catalog.json') return mergedResponse(response, { 'content-type': 'application/json; charset=utf-8', 'access-control-allow-origin': '*' });
    return response;
  },
};
