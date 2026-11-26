// Planted bug 12: this looks for a theme picker that the page no longer has, so it
// throws a TypeError as soon as the page loads. The page itself still works.
const picker = document.getElementById("theme-picker");
picker.addEventListener("change", () => {
  document.documentElement.dataset.theme = picker.value;
});
