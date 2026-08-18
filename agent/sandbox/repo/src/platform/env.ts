/**
 * Environment access. Centralised so that a missing variable is a single
 * grep away rather than scattered across process.env reads.
 */

export function readEnv(name: string): string | undefined {
  const value = process.env[name];
  return value === "" ? undefined : value;
}

export function requireEnv(name: string): string {
  const value = readEnv(name);
  if (!value) {
    throw new Error(`missing required environment variable: ${name}`);
  }
  return value;
}
