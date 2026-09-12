/** Deterministic unique suffix so parallel workers never collide. */
export function uniqueSuffix(): string {
  return [Date.now().toString(36), Math.random().toString(36).slice(2, 7)].join('-');
}

export function buildAddress(overrides: Partial<Record<string, string>> = {}) {
  return { line1: '1 Test Street', city: 'Testville', postcode: 'TE1 1ST', ...overrides };
}
