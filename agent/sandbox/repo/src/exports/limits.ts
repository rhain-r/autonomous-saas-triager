/**
 * Export size ceilings. These are product limits, published to customers, not
 * infrastructure limits — raising them is a pricing decision.
 */

export const MAX_EXPORT_BYTES = 50 * 1024 * 1024;
export const MAX_EXPORT_ROWS = 1_000_000;

export function exceedsExportLimit(sizeBytes: number, rows: number): boolean {
  return sizeBytes > MAX_EXPORT_BYTES || rows > MAX_EXPORT_ROWS;
}
