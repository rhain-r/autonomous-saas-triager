/**
 * Structured logger. Emits one key=value line per event; the shape is what the
 * log search tooling parses, so field names are effectively an API.
 */

type Level = "debug" | "info" | "warn" | "error";

interface Fields {
  code?: string;
  user?: string;
  detail?: string;
  [key: string]: unknown;
}

function emit(level: Level, service: string, message: string, fields: Fields) {
  const parts = Object.entries(fields)
    .filter(([, v]) => v !== undefined)
    .map(([k, v]) => `${k}="${String(v)}"`);
  process.stdout.write(
    `${new Date().toISOString()} ${level.toUpperCase()} ${service} msg="${message}" ${parts.join(" ")}\n`,
  );
}

export const logger = {
  debug: (msg: string, f: Fields = {}) => emit("debug", "app", msg, f),
  info: (msg: string, f: Fields = {}) => emit("info", "app", msg, f),
  warn: (msg: string, f: Fields = {}) => emit("warn", "app", msg, f),
  error: (msg: string, f: Fields = {}) => emit("error", "app", msg, f),
};
