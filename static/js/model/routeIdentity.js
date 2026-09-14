// Model names are not routes: different endpoints may serve the same name.
export function modelRouteKey(route) {
  return `${route.endpointId || route.endpoint_id || route.url || route.epName || 'model'}::${route.mid || route.model || ''}`;
}

function endpointAddress(route) {
  try { return new URL(route.url).host; } catch (_) { return ''; }
}

export function modelEndpointLabel(route, catalog) {
  const name = route.epName || route.endpoint_name || endpointAddress(route) || route.endpointId || route.endpoint_id || 'Endpoint';
  const peers = catalog.filter(item => (item.epName || item.endpoint_name || endpointAddress(item) || item.endpointId || item.endpoint_id || 'Endpoint') === name);
  const identity = item => item.endpointId || item.endpoint_id || item.url;
  // Always show the full routing address, not only when names collide.
  // Credentials and query parameters are not useful endpoint labels.
  let fullAddress = '';
  try { const parsed = new URL(route.url); fullAddress = parsed.origin + parsed.pathname; } catch (_) {}
  const base = fullAddress ? `${name} · ${fullAddress}` : name;
  if (new Set(peers.map(identity)).size <= 1) return base;
  const address = endpointAddress(route);
  if (address && new Set(peers.filter(item => endpointAddress(item) === address).map(identity)).size <= 1) return base;
  let location = '';
  try { const parsed = new URL(route.url); location = parsed.origin + parsed.pathname; } catch (_) {}
  return `${base} · ${route.endpointId || route.endpoint_id || location || 'unknown route'}`;
}

export function isRouteFavorite(favorites, route) {
  return favorites.includes(modelRouteKey(route)) || favorites.includes(route.mid);
}

export function toggleRouteFavorite(favorites, route, catalog) {
  const wasFavorite = isRouteFavorite(favorites, route), key = modelRouteKey(route);
  // A legacy bare-name favorite meant every endpoint. Materialize its current
  // routes before toggling one, preserving the other endpoint preferences.
  const next = favorites.flatMap(value => value === route.mid
    ? catalog.filter(item => item.mid === route.mid).map(modelRouteKey) : [value]);
  return [...new Set(wasFavorite ? next.filter(value => value !== key) : [...next, key])];
}

export function resolveSavedModelRoutes(saved, catalog, expandLegacy = false) {
  const result = [], seen = new Set();
  for (const value of saved) {
    const exact = catalog.find(item => modelRouteKey(item) === value);
    const legacy = exact ? [] : catalog.filter(item => item.mid === value);
    // An old ambiguous Recent pick has no endpoint information. Do not invent
    // an endpoint by silently restoring the first catalog entry.
    const matches = exact ? [exact] : (expandLegacy || legacy.length === 1 ? legacy : []);
    for (const item of matches) {
      const key = modelRouteKey(item);
      if (!seen.has(key)) { seen.add(key); result.push(item); }
    }
  }
  return result;
}
