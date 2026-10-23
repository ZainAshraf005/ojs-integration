// Copy buttons: <button data-copy="text">  ·  Table search: <input data-filter="#table-id">
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy]");
  if (!button) return;
  const text = button.dataset.copy;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  const label = button.textContent;
  button.textContent = "Copied";
  button.classList.add("copied");
  setTimeout(() => { button.textContent = label; button.classList.remove("copied"); }, 1500);
});

document.querySelectorAll("[data-filter]").forEach((input) => {
  const rows = document.querySelectorAll(`${input.dataset.filter} tbody tr`);
  input.addEventListener("input", () => {
    const q = input.value.trim().toLowerCase();
    rows.forEach((row) => { row.hidden = q && !row.textContent.toLowerCase().includes(q); });
  });
});
