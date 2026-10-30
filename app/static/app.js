// Copy buttons: <button data-copy="text">  ·  Table search: <input data-filter="#table-id">  ·  Theme: <button data-theme-toggle>
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

// Theme toggle: <button data-theme-toggle>. Light unless "dark" is saved (read early by the inline script in <head>).
document.addEventListener("click", (event) => {
  if (!event.target.closest("[data-theme-toggle]")) return;
  const root = document.documentElement;
  const theme = root.dataset.theme === "dark" ? "light" : "dark";
  if (theme === "dark") root.dataset.theme = "dark";
  else delete root.dataset.theme;
  try { localStorage.setItem("theme", theme); } catch {}
});
