/**
 * The admin session, held in this tab only.
 *
 * A session is the server's signed token, kept in memory and mirrored to
 * `sessionStorage` so a reload during the same working sitting does not force
 * a re-login -- but never in `localStorage`: this token grants merchant
 * control over live spending rules, and it should not outlive the tab that
 * signed in, let alone survive on the machine after the browser closes.
 *
 * There is no refresh here. When the token's own expiry passes, the next
 * mandate-edit call gets a 401 from the server and the UI asks for another
 * login -- the client never extends a session on its own say-so.
 */

const STORAGE_KEY = "admin-session"

interface StoredSession {
  token: string
  expiresAt: number  // epoch seconds
  username: string
}

let session: StoredSession | null = load()

function load(): StoredSession | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as StoredSession
    if (typeof parsed.expiresAt !== "number" || Date.now() / 1000 >= parsed.expiresAt) {
      sessionStorage.removeItem(STORAGE_KEY)
      return null
    }
    return parsed
  } catch {
    return null
  }
}

function persist(next: StoredSession | null) {
  session = next
  try {
    if (next) sessionStorage.setItem(STORAGE_KEY, JSON.stringify(next))
    else sessionStorage.removeItem(STORAGE_KEY)
  } catch {
    // Storage can be unavailable (private mode, quota). The in-memory copy
    // still works for the rest of this page load; it just will not survive
    // a reload -- a lesser failure than the login silently not working.
  }
}

export function setAdminSession(token: string, expiresInSeconds: number, username: string) {
  persist({ token, expiresAt: Date.now() / 1000 + expiresInSeconds, username })
}

export function clearAdminSession() {
  persist(null)
}

/** The bearer token, or null if there is no session or it has expired. */
export function adminToken(): string | null {
  if (session && Date.now() / 1000 >= session.expiresAt) {
    persist(null)
  }
  return session?.token ?? null
}

export function adminUsername(): string | null {
  return adminToken() ? session?.username ?? null : null
}

export function isAdminSignedIn(): boolean {
  return adminToken() !== null
}
