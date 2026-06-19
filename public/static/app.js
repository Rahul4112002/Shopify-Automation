import { upload } from "https://esm.sh/@vercel/blob/client";

const fileInputs = document.querySelectorAll('input[type="file"]');
const uploadForm = document.querySelector(".upload-form");
const runButton = document.querySelector(".run-button");
const progressBox = document.querySelector(".upload-progress");
const progressText = document.querySelector("[data-progress-text]");

const fieldLabels = {
  content_master: "Content Master",
  gs1: "GS1",
  dropbox_links: "Dropbox Image Links",
  product_listing: "Product Listing",
};

const fieldExtensions = {
  content_master: [".xlsx", ".xls"],
  gs1: [".xlsx", ".xls", ".xlsb"],
  dropbox_links: [".xlsx", ".xls"],
  product_listing: [".csv", ".xlsx", ".xls"],
};

function setBusy(isBusy, message = "") {
  if (runButton) {
    runButton.disabled = isBusy;
    runButton.querySelector("span").textContent = isBusy ? "Processing..." : "Run Full Pipeline";
  }
  if (progressBox) {
    progressBox.hidden = !isBusy;
  }
  if (progressText && message) {
    progressText.textContent = message;
  }
}

function validateFile(input) {
  const file = input.files?.[0];
  if (!file) {
    throw new Error(`${fieldLabels[input.name] || input.name} file is required.`);
  }

  const allowed = fieldExtensions[input.name] || [];
  const lowerName = file.name.toLowerCase();
  if (!allowed.some((extension) => lowerName.endsWith(extension))) {
    throw new Error(`${fieldLabels[input.name] || input.name} must be one of: ${allowed.join(", ")}`);
  }
  return file;
}

function collectOverrides(form) {
  const data = new FormData();
  ["content_sheet", "gs1_sheet", "dropbox_sheet"].forEach((name) => {
    const value = form.querySelector(`[name="${name}"]`)?.value?.trim();
    if (value) {
      data.append(name, value);
    }
  });
  return data;
}

async function uploadFilesToBlob(form) {
  const uploads = {};
  const inputs = Array.from(fileInputs);
  const jobId = crypto.randomUUID();

  for (let index = 0; index < inputs.length; index += 1) {
    const input = inputs[index];
    const file = validateFile(input);
    const label = fieldLabels[input.name] || input.name;
    setBusy(true, `Uploading ${label} (${index + 1}/${inputs.length})...`);

    const blob = await upload(`shopify-listing/input/${jobId}/${input.name}-${file.name}`, file, {
      access: "public",
      handleUploadUrl: form.dataset.blobUploadUrl || "/api/blob-upload",
      clientPayload: JSON.stringify({
        field: input.name,
        jobId,
        filename: file.name,
      }),
    });

    uploads[input.name] = {
      url: blob.url,
      downloadUrl: blob.downloadUrl || blob.url,
      pathname: blob.pathname,
      filename: file.name,
      size: file.size,
    };
  }
  return uploads;
}

async function submitBlobForm(form) {
  const uploads = await uploadFilesToBlob(form);
  setBusy(true, "Running Pandas pipeline...");

  const data = collectOverrides(form);
  data.append("blob_uploads", JSON.stringify(uploads));

  const response = await fetch(form.action, {
    method: "POST",
    body: data,
  });
  const html = await response.text();

  if (!response.ok && !html) {
    throw new Error("Pipeline failed before a report page could be returned.");
  }

  document.open();
  document.write(html);
  document.close();
}

fileInputs.forEach((input) => {
  input.addEventListener("change", () => {
    const card = input.closest(".upload-card");
    if (!card) {
      return;
    }
    card.classList.toggle("has-file", input.files.length > 0);
  });
});

if (uploadForm) {
  uploadForm.addEventListener("submit", async (event) => {
    const blobEnabled = uploadForm.dataset.blobEnabled === "1";

    if (!blobEnabled) {
      const maxUploadMb = Number(uploadForm.dataset.maxUploadMb || "0");
      if (!maxUploadMb) {
        return;
      }

      const totalBytes = Array.from(fileInputs).reduce((total, input) => {
        return total + Array.from(input.files || []).reduce((sum, file) => sum + file.size, 0);
      }, 0);
      const totalMb = totalBytes / (1024 * 1024);

      if (totalMb > maxUploadMb) {
        event.preventDefault();
        alert(
          `Selected files are ${totalMb.toFixed(1)} MB total, but this deployment accepts ${maxUploadMb} MB. ` +
            "Enable Vercel Blob for large production files."
        );
      }
      return;
    }

    event.preventDefault();
    try {
      setBusy(true, "Preparing Vercel Blob uploads...");
      await submitBlobForm(uploadForm);
    } catch (error) {
      setBusy(false);
      alert(error.message || "Upload failed. Please try again.");
    }
  });
}
