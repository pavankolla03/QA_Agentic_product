export type TestUser = { username: string; password: string; role: string };

/** Credentials always come from the environment, never from source. */
export function getUser(kind: 'standard' | 'admin' | 'readonly'): TestUser {
  const map: Record<string, TestUser> = {
    standard: { username: process.env.STD_USER ?? 'std.user', password: process.env.STD_PASS ?? '', role: 'user' },
    admin: { username: process.env.ADMIN_USER ?? 'admin.user', password: process.env.ADMIN_PASS ?? '', role: 'admin' },
    readonly: { username: process.env.RO_USER ?? 'ro.user', password: process.env.RO_PASS ?? '', role: 'viewer' },
  };
  return map[kind];
}
