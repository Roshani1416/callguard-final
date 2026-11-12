import { auth } from './firebase';

const base = (import.meta.env.VITE_API_BASE_URL || (import.meta.env.DEV ? 'http://127.0.0.1:8000' : '')).replace(/\/$/, '');

export async function apiFetch(path: string, options: RequestInit = {}) {
  await auth.authStateReady();
  if (!auth.currentUser) throw new Error('Sign in to continue.');
  if (!base && import.meta.env.PROD) throw new Error('Backend URL is missing from the frontend build.');
  const headers = new Headers(options.headers);
  headers.set('Authorization', 'Bearer ' + await auth.currentUser.getIdToken());
  const response = await fetch(base + path, { ...options, headers });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === 'string' ? body.detail : 'Request failed (' + response.status + ').');
  }
  return response;
}
