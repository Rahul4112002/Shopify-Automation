const form = document.querySelector(".upload-form");
const busy = document.querySelector("[data-busy]");
const busyText = document.querySelector("[data-busy-text]");
const formError = document.querySelector("[data-form-error]");
const runButton = document.querySelector(".run-button");
const runLabel = document.querySelector("[data-run-label]");

const fieldExtensions = {
  content_master: [".xlsx", ".xls"],
  gs1: [".xlsx", ".xls", ".xlsb"],
  dropbox_links: [".xlsx", ".xls", ".csv"],
  product_listing: [".csv", ".xlsx", ".xls"],
};

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function cardFor(input) {
  return input.closest("[data-upload-card]");
}

function labelFor(input) {
  return cardFor(input)?.dataset.label || input.name;
}

function isRequired(input) {
  return cardFor(input)?.dataset.required === "1";
}

function showError(message) {
  if (!formError) {
    alert(message);
    return;
  }
  formError.textContent = message;
  formError.hidden = !message;
}

function setBusy(isBusy, message = "") {
  if (busy) busy.hidden = !isBusy;
  if (busyText && message) busyText.textContent = message;
  if (runButton) runButton.disabled = isBusy;
  if (runLabel) runLabel.textContent = isBusy ? "Working…" : "Generate listing file";
}

function updateCard(input) {
  const card = cardFor(input);
  if (!card) return;
  const file = input.files?.[0];
  const nameEl = card.querySelector("[data-file-name]");
  const metaEl = card.querySelector("[data-file-meta]");
  const clearBtn = card.querySelector("[data-clear]");

  if (!card.dataset.defaultName) card.dataset.defaultName = nameEl.innerHTML;
  if (!card.dataset.defaultMeta) card.dataset.defaultMeta = metaEl.textContent;

  card.classList.toggle("has-file", Boolean(file));
  card.classList.remove("is-invalid");
  if (file) {
    nameEl.textContent = file.name;
    metaEl.textContent = `${formatBytes(file.size)} · ready`;
  } else {
    nameEl.innerHTML = card.dataset.defaultName;
    metaEl.textContent = card.dataset.defaultMeta;
  }
  if (clearBtn) clearBtn.hidden = !file;
}

function validateFile(input) {
  const file = input.files?.[0];
  if (!file) {
    if (isRequired(input)) throw new Error(`Please upload the ${labelFor(input)} file.`);
    return null;
  }
  const allowed = fieldExtensions[input.name] || [];
  const lowerName = file.name.toLowerCase();
  if (!allowed.some((extension) => lowerName.endsWith(extension))) {
    throw new Error(`${labelFor(input)} must be one of: ${allowed.join(", ")}`);
  }
  return file;
}

function validateAll(inputs) {
  let firstError = null;
  inputs.forEach((input) => {
    try {
      validateFile(input);
      cardFor(input)?.classList.remove("is-invalid");
    } catch (error) {
      cardFor(input)?.classList.add("is-invalid");
      firstError = firstError || error;
    }
  });
  if (firstError) throw firstError;
}

function setupDropzones() {
  document.querySelectorAll("[data-upload-card]").forEach((card) => {
    const input = card.querySelector('input[type="file"]');
    const zone = card.querySelector(".dropzone");
    const clearBtn = card.querySelector("[data-clear]");
    if (!input || !zone) return;

    input.addEventListener("change", () => {
      updateCard(input);
      showError("");
    });

    ["dragenter", "dragover"].forEach((type) =>
      zone.addEventListener(type, (event) => {
        event.preventDefault();
        zone.classList.add("is-dragover");
      })
    );
    ["dragleave", "drop"].forEach((type) =>
      zone.addEventListener(type, (event) => {
        event.preventDefault();
        zone.classList.remove("is-dragover");
      })
    );
    zone.addEventListener("drop", (event) => {
      const files = event.dataTransfer?.files;
      if (!files || !files.length) return;
      const transfer = new DataTransfer();
      transfer.items.add(files[0]);
      input.files = transfer.files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });

    clearBtn?.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      input.value = "";
      updateCard(input);
    });

    updateCard(input);
  });
}

function collectFields(formEl) {
  const data = new FormData();
  ["content_sheet", "gs1_sheet", "dropbox_sheet"].forEach((name) => {
    const value = formEl.querySelector(`[name="${name}"]`)?.value?.trim();
    if (value) data.append(name, value);
  });
  const aiToggle = formEl.querySelector('input[type="checkbox"][name="use_ai"]');
  if (aiToggle) data.append("use_ai", aiToggle.checked && !aiToggle.disabled ? "1" : "0");
  return data;
}

async function uploadFilesToBlob(formEl, inputs) {
  const { upload } = await import("https://esm.sh/@vercel/blob/client");
  const uploads = {};
  const jobId = crypto.randomUUID();
  const selected = inputs.filter((input) => input.files?.[0]);

  for (let index = 0; index < selected.length; index += 1) {
    const input = selected[index];
    const file = validateFile(input);
    const label = `Uploading ${labelFor(input)} (${index + 1} of ${selected.length})`;
    setBusy(true, `${label}…`);

    // The Blob client retries failed uploads silently; abort if nothing moves for 90 seconds.
    const controller = new AbortController();
    let lastProgress = Date.now();
    const stallTimer = setInterval(() => {
      if (Date.now() - lastProgress > 90_000) controller.abort();
    }, 5_000);

    let blob;
    try {
      blob = await upload(`shopify-listing/input/${jobId}/${input.name}-${file.name}`, file, {
        access: formEl.dataset.blobAccess === "public" ? "public" : "private",
        handleUploadUrl: formEl.dataset.blobUploadUrl || "/api/blob-upload",
        clientPayload: JSON.stringify({ field: input.name, jobId, filename: file.name }),
        abortSignal: controller.signal,
        onUploadProgress: ({ percentage }) => {
          lastProgress = Date.now();
          setBusy(true, `${label}… ${Math.round(percentage || 0)}%`);
        },
      });
    } catch (error) {
      if (controller.signal.aborted) {
        throw new Error(
          `Uploading ${labelFor(input)} stalled. Check the internet connection and the Vercel Blob store settings, then try again.`
        );
      }
      throw new Error(`Uploading ${labelFor(input)} failed: ${error.message || error}`);
    } finally {
      clearInterval(stallTimer);
    }

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

async function submitWithBlob(formEl, inputs) {
  const uploads = await uploadFilesToBlob(formEl, inputs);
  setBusy(true, "Matching SKUs, writing titles and building the CSV…");

  const data = collectFields(formEl);
  data.append("blob_uploads", JSON.stringify(uploads));
  const response = await fetch(formEl.action, { method: "POST", body: data });
  const html = await response.text();
  if (!html) throw new Error("The server did not return a result page. Please try again.");

  document.open();
  document.write(html);
  document.close();
}

function setupForm() {
  if (!form) return;
  const inputs = Array.from(form.querySelectorAll('input[type="file"]'));

  form.addEventListener("submit", async (event) => {
    showError("");
    try {
      validateAll(inputs);
    } catch (error) {
      event.preventDefault();
      showError(error.message);
      return;
    }

    const aiToggle = form.querySelector('input[type="checkbox"][name="use_ai"]');
    const aiFallback = form.querySelector("[data-ai-fallback]");
    if (aiToggle && aiFallback) aiFallback.disabled = aiToggle.checked && !aiToggle.disabled;

    if (form.dataset.blobEnabled !== "1") {
      const maxUploadMb = Number(form.dataset.maxUploadMb || "0");
      const totalBytes = inputs.reduce((sum, input) => sum + (input.files?.[0]?.size || 0), 0);
      if (maxUploadMb && totalBytes / (1024 * 1024) > maxUploadMb) {
        event.preventDefault();
        showError(
          `Selected files are ${(totalBytes / (1024 * 1024)).toFixed(1)} MB in total; this deployment accepts ${maxUploadMb} MB.`
        );
        return;
      }
      setBusy(true, "Matching SKUs, writing titles and building the CSV…");
      return; // normal form post
    }

    event.preventDefault();
    try {
      setBusy(true, "Preparing uploads…");
      await submitWithBlob(form, inputs);
    } catch (error) {
      setBusy(false);
      showError(error.message || "Upload failed. Please try again.");
    }
  });

  // If the user navigates back to this page, make sure the overlay is not stuck.
  window.addEventListener("pageshow", () => setBusy(false));
}

function setupChecklist() {
  const box = document.querySelector("[data-checklist]");
  if (!box) return;
  const boxes = Array.from(box.querySelectorAll('input[type="checkbox"]'));
  const progress = box.querySelector("[data-checklist-progress]");
  const update = () => {
    const done = boxes.filter((item) => item.checked).length;
    if (progress) {
      progress.textContent = done === boxes.length ? "All checked, ready to import" : `${done} of ${boxes.length} checked`;
    }
    box.classList.toggle("is-complete", done === boxes.length);
  };
  boxes.forEach((item) => item.addEventListener("change", update));
  update();
  document.getElementById("results")?.scrollIntoView({ behavior: "smooth", block: "start" });
}

setupDropzones();
setupForm();
setupChecklist();
