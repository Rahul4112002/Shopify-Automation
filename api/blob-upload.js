import { handleUpload } from "@vercel/blob/client";

const ALLOWED_EXTENSIONS = {
  content_master: [".xlsx", ".xls"],
  gs1: [".xlsx", ".xls", ".xlsb"],
  dropbox_links: [".xlsx", ".xls", ".csv"],
  product_listing: [".csv", ".xlsx", ".xls"],
};

function extensionAllowed(filename, field) {
  const lowerName = String(filename || "").toLowerCase();
  return (ALLOWED_EXTENSIONS[field] || []).some((extension) => lowerName.endsWith(extension));
}

function parsePayload(payload) {
  try {
    return JSON.parse(payload || "{}");
  } catch {
    return {};
  }
}

function blobReadWriteToken() {
  return process.env.BLOB_READ_WRITE_TOKEN || process.env.BLOB_READ_WRITE_TOKEN_READ_WRITE_TOKEN;
}

export async function POST(request) {
  try {
    const token = blobReadWriteToken();
    if (!token) {
      throw new Error("Missing Vercel Blob read-write token.");
    }

    const body = await request.json();
    const response = await handleUpload({
      body,
      request,
      token,
      onBeforeGenerateToken: async (pathname, clientPayload) => {
        const payload = parsePayload(clientPayload);
        const field = payload.field;
        const filename = payload.filename || pathname;

        if (!ALLOWED_EXTENSIONS[field] || !extensionAllowed(filename, field)) {
          throw new Error("Unsupported upload type.");
        }

        return {
          allowedContentTypes: [
            "text/csv",
            "application/csv",
            "application/octet-stream",
            "application/vnd.ms-excel",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            // Browsers report .xlsb in lower case; Vercel compares content types case-sensitively.
            "application/vnd.ms-excel.sheet.binary.macroEnabled.12",
            "application/vnd.ms-excel.sheet.binary.macroenabled.12",
            "application/x-msexcel",
            "text/plain",
          ],
          addRandomSuffix: true,
          tokenPayload: JSON.stringify({
            field,
            filename,
            jobId: payload.jobId,
          }),
        };
      },
      onUploadCompleted: async ({ blob, tokenPayload }) => {
        console.log("Blob upload completed", blob.pathname, tokenPayload);
      },
    });

    return Response.json(response);
  } catch (error) {
    return Response.json(
      {
        error: error instanceof Error ? error.message : "Blob upload failed.",
      },
      { status: 400 },
    );
  }
}
