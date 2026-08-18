/**
 * Thin key-value facade over Redis. Values are JSON-serialised.
 */

const memory = new Map<string, string>();

export const store = {
  async get<T>(key: string): Promise<T | null> {
    const raw = memory.get(key);
    return raw ? (JSON.parse(raw) as T) : null;
  },

  async put(key: string, value: unknown): Promise<void> {
    memory.set(key, JSON.stringify(value));
  },
};
