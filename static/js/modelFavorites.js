let state = { favorites: [], revision: 0, loaded: false };
let inflight = null;
const LEGACY_FAVORITES_KEY = 'odysseus-model-favorites';

export function favoritesSnapshot() { return state.favorites.slice(); }

function legacySnapshot() {
  try {
    const raw = JSON.parse(localStorage.getItem(LEGACY_FAVORITES_KEY) || '[]');
    return Array.isArray(raw) ? raw.map(String).filter(Boolean) : [];
  } catch (_) { return []; }
}

/** Return browser-local favorites without ever applying them implicitly. */
export function legacyFavoritesSnapshot() { return legacySnapshot(); }

/**
 * Explicitly import the old per-browser list into the owner-scoped store.
 * Ambiguous bare model IDs are expanded to every matching catalog route only
 * after the user clicks the import action; no account receives hidden data.
 */
export async function importLegacyFavorites(catalog = []) {
  const legacy = legacySnapshot();
  if (!legacy.length) return favoritesSnapshot();
  const routes = [];
  for (const value of legacy) {
    const matches = value.includes('::')
      ? catalog.filter(item => `${item.endpointId || item.endpoint_id || item.url || 'model'}::${item.mid || item.model || ''}` === value)
      : catalog.filter(item => (item.mid || item.model || '') === value);
    for (const item of matches) {
      const key = `${item.endpointId || item.endpoint_id || item.url || 'model'}::${item.mid || item.model || ''}`;
      if (!routes.includes(key)) routes.push(key);
    }
  }
  for (const key of routes) {
    if (state.favorites.includes(key)) continue;
    try { await setModelFavorite(key, true); }
    catch (error) {
      // A concurrent browser may have advanced the revision. Refresh once and
      // retry this exact key; never overwrite the newer snapshot wholesale.
      if (/Favorites changed/.test(String(error?.message || ''))) {
        await refreshModelFavorites();
        if (!state.favorites.includes(key)) await setModelFavorite(key, true);
      } else throw error;
    }
  }
  try { localStorage.removeItem(LEGACY_FAVORITES_KEY); } catch (_) {}
  return favoritesSnapshot();
}

export async function refreshModelFavorites() {
  if (inflight) return inflight;
  inflight = fetch('/api/prefs/model-favorites/snapshot', { credentials: 'same-origin', cache: 'no-store' })
    .then(async response => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      state = { favorites: Array.isArray(data.favorites) ? data.favorites : [], revision: Number(data.revision || 0), loaded: true };
      window.dispatchEvent(new CustomEvent('odysseus:model-favorites', { detail: state }));
      return favoritesSnapshot();
    }).finally(() => { inflight = null; });
  return inflight;
}

export async function setModelFavorite(key, favorite) {
  const response = await fetch('/api/prefs/model-favorites/toggle', {
    method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key, favorite, expected_revision: state.revision }),
  });
  if (response.status === 409) { await refreshModelFavorites(); throw new Error('Favorites changed in another browser'); }
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const data = await response.json();
  state = { favorites: data.favorites || [], revision: Number(data.revision || 0), loaded: true };
  window.dispatchEvent(new CustomEvent('odysseus:model-favorites', { detail: state }));
  return favoritesSnapshot();
}

['focus', 'pageshow'].forEach(type => window.addEventListener(type, refreshModelFavorites));
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') void refreshModelFavorites(); });
void refreshModelFavorites();
