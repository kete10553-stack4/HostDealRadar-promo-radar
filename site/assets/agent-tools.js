(() => {
  const api = navigator.modelContext;
  if (!api || typeof api.registerTool !== 'function') return;
  const controller = new AbortController();
  api.registerTool({
    name: 'hostdealradar_lookup',
    description: 'Read bounded public HostDealRadar records by exact provider ID or record slug.',
    inputSchema: { type: 'object', additionalProperties: false, oneOf: [{ required: ['provider'], properties: { provider: { type: 'string' } } }, { required: ['slug'], properties: { slug: { type: 'string' } } }] },
    execute: async (input) => {
      const params = new URLSearchParams(); if (typeof input.provider === 'string') params.set('provider', input.provider); if (typeof input.slug === 'string') params.set('slug', input.slug);
      const response = await fetch('/api/agent/lookup?' + params.toString(), { headers: { accept: 'application/json' } });
      return await response.json();
    }
  }, { signal: controller.signal });
})();
