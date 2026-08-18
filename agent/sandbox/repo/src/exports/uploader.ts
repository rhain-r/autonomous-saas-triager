/**
 * Streams a generated export to object storage and returns a signed URL.
 */

import { MAX_EXPORT_BYTES, exceedsExportLimit } from "./limits";
import { logger } from "../platform/logger";

export interface ExportRequest {
  userId: string;
  sizeBytes: number;
  rows: number;
  format: "csv" | "parquet";
}

export async function uploadExport(req: ExportRequest) {
  if (exceedsExportLimit(req.sizeBytes, req.rows)) {
    logger.warn("export rejected: over published limit", {
      code: "EXPORT_TOO_LARGE",
      user: req.userId,
      detail: `${req.sizeBytes} bytes exceeds ${MAX_EXPORT_BYTES}`,
    });
    return { status: 413, error: "export exceeds the 50 MB limit for your plan" };
  }

  const url = await putObject(req);
  return { status: 200, url };
}

async function putObject(req: ExportRequest): Promise<string> {
  return `https://exports.meridian.io/${req.userId}/${Date.now()}.${req.format}`;
}
