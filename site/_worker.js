const LINK_HEADER = [
  '</.well-known/api-catalog>; rel="api-catalog"',
  '</openapi.json>; rel="service-desc"',
  '</ai/>; rel="service-doc"',
  '</.well-known/ai-catalog.json>; rel="describedby"'
].join(', ');
const WITHDRAWN_PROVIDER_PATHS = new Set(["/providers/infomaniak/", "/providers/wix/"]);
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
async function publicLookup(env, provider, slug) {
  if ((!provider && !slug) || (provider && slug)) return { status: 400, body: { error: 'invalid_query', message: 'Supply exactly one of provider or slug.' } };
  const records = await (await asset(env, new Request('https://hostdealradar.com/agent-data.json'), '/agent-data.json')).json();
  const matches = slug ? records.filter((record) => record.id === slug) : records.filter((record) => record.provider.id === provider || record.provider.name.toLowerCase() === provider);
  if (!matches.length) return { status: 404, body: { status: 'not_found', query: slug ? { slug } : { provider }, records: [], message: 'No public HostDealRadar record matched this exact identifier.' } };
  return { status: 200, body: { status: 'ok', query: slug ? { slug } : { provider }, record_count: Math.min(matches.length, 25), records: matches.slice(0, 25), limitations: 'This read-only API reports published source records. It does not test checkout, availability, eligibility, or provider performance.' } };
}
function mcpReply(id, result) { return json({ jsonrpc: '2.0', id: id === undefined ? null : id, result }); }
function mcpError(id, code, message) { return json({ jsonrpc: '2.0', id: id === undefined ? null : id, error: { code, message } }); }
async function mcp(request, env) {
  if (request.method !== 'POST') return json({ error: 'method_not_allowed', message: 'Use POST for this Streamable HTTP MCP endpoint.' }, 405, { allow: 'POST' });
  let message; try { message = await request.json(); } catch { return mcpError(null, -32700, 'Parse error'); }
  if (!message || message.jsonrpc !== '2.0' || typeof message.method !== 'string') return mcpError(message && message.id, -32600, 'Invalid Request');
  if (message.method === 'initialize') return mcpReply(message.id, { protocolVersion: '2025-03-26', capabilities: { tools: {} }, serverInfo: { name: 'hostdealradar-public-lookup', version: '1.0.0' } });
  if (message.method === 'tools/list') return mcpReply(message.id, { tools: [{ name: 'hostdealradar_lookup', description: 'Read bounded public HostDealRadar records by an exact provider ID or record slug.', inputSchema: { type: 'object', additionalProperties: false, oneOf: [{ required: ['provider'], properties: { provider: { type: 'string', description: 'Exact provider ID or name.' } } }, { required: ['slug'], properties: { slug: { type: 'string', description: 'Exact public record ID.' } } }] } }] });
  if (message.method === 'tools/call') {
    const params = message.params || {}; if (params.name !== 'hostdealradar_lookup') return mcpError(message.id, -32602, 'Unknown tool');
    const args = params.arguments || {}; const result = await publicLookup(env, typeof args.provider === 'string' ? args.provider.trim().toLowerCase() : '', typeof args.slug === 'string' ? args.slug.trim() : '');
    return mcpReply(message.id, { content: [{ type: 'text', text: JSON.stringify(result.body) }], structuredContent: result.body, isError: result.status !== 200 });
  }
  return mcpError(message.id, -32601, 'Method not found');
}
export default {
  async fetch(request, env) {
    const url = new URL(request.url); const path = url.pathname;
    const normalizedPath = path.endsWith('/') ? path : path + '/';
    if (WITHDRAWN_PROVIDER_PATHS.has(normalizedPath)) {
      return new Response('This provider page is not published because no verifiable official source record is available.', { status: 410, headers: { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' } });
    }
    const oldRecord = path.match(/^\/deals\/([a-z0-9-]+)\/?$/);
    if (oldRecord) {
      const result = await publicLookup(env, '', oldRecord[1]);
      if (result.status === 200) return Response.redirect(new URL(result.body.records[0].record_url, url.origin), 301);
    }
    if (path === '/api/agent/lookup') {
      if (request.method !== 'GET') return json({ error: 'method_not_allowed', message: 'Use GET for this read-only endpoint.' }, 405, { allow: 'GET' });
      const provider = (url.searchParams.get('provider') || '').trim().toLowerCase(); const slug = (url.searchParams.get('slug') || '').trim();
      const result = await publicLookup(env, provider, slug); return json(result.body, result.status);
    }
    if (path === '/mcp') return mcp(request, env);
    if (['/agent-auth/authorize', '/agent-auth/token', '/agent-auth/register', '/agent-auth/claim', '/agent-auth/revoke'].includes(path)) return unavailable();
    const specialAssets = { '/ai/': ['/ai/index.md', 'text/markdown; charset=utf-8'], '/ai/index.ilang': ['/ai/index.ilang', 'text/plain; charset=utf-8'], '/.well-known/api-catalog': ['/.well-known/api-catalog.json', 'application/linkset+json; charset=utf-8'], '/.well-known/oauth-authorization-server': ['/.well-known/oauth-authorization-server', 'application/json; charset=utf-8'], '/.well-known/oauth-protected-resource': ['/.well-known/oauth-protected-resource', 'application/json; charset=utf-8'], '/.well-known/jwks.json': ['/.well-known/jwks.json', 'application/json; charset=utf-8'], '/.well-known/mcp/server-card.json': ['/.well-known/mcp/server-card.json', 'application/json; charset=utf-8'] };
    if (specialAssets[path]) { const [assetPath, contentType] = specialAssets[path]; return mergedResponse(await asset(env, request, assetPath), { 'content-type': contentType, 'access-control-allow-origin': '*' }); }
    if (path === '/' && wantsMarkdown(request)) return mergedResponse(await asset(env, request, '/ai/index.md'), { 'content-type': 'text/markdown; charset=utf-8', 'vary': 'Accept', 'link': LINK_HEADER });
    const response = await env.ASSETS.fetch(request);
    if (path === '/') return mergedResponse(response, { 'vary': 'Accept', 'link': LINK_HEADER });
    if (path === '/.well-known/ai-catalog.json') return mergedResponse(response, { 'content-type': 'application/json; charset=utf-8', 'access-control-allow-origin': '*' });
    return response;
  },
};
