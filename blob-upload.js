import { handleUpload } from "@vercel/blob/client";

const ALLOWED_EXTENSIONS = {
  content_master: [".xlsx", ".xls"],
  gs1: [".xlsx", ".xls", ".xlsb"],
  dropbox_links: [".xlsx", ".xls"],
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

export default {
  async fetch(request) {
    if (request.method !== "POST") {
      return Response.json({ error: "Method not allowed." }, { status: 405 });
    }

    try {
      const body = await request.json();
      const response = await handleUpload({
        body,
        request,
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
              "application/vnd.ms-excel.sheet.binary.macroEnabled.12",
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
  },
};
