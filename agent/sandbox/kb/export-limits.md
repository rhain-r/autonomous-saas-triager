---
article_id: KB-0031
title: Export size limits
tags: [export, csv, parquet, limits, download, 413]
updated: 2026-07-18
---

# Export size limits

Every plan above Free can export up to **50 MB** or **1,000,000 rows** per
export, whichever is reached first. Requests above either ceiling are rejected
with HTTP 413 and the message *"export exceeds the 50 MB limit for your plan"*.

These are product limits rather than infrastructure limits. They are the same
on Team and Business plans.

## Exporting a larger dataset

- **Narrow the date range.** Most exports shrink below the limit at 90 days.
- **Select fewer columns.** The column picker is in the export dialog.
- **Split by segment** and export each segment separately.
- **Use the Bulk Export API** for datasets above 1M rows. It writes directly to
  your own S3 bucket and has no size ceiling.

## Enterprise

Enterprise contracts can raise these ceilings. Raising a limit is a change to
your plan, so it goes through your account team rather than support.
