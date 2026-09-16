let state = { favorites: [], revision: 0, loaded: false };
let inflight = null;

export function favoritesSnapshot() { return state.favorites.slice(); }

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
