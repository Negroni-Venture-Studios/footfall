// Stops a second tap on "Add task" from adding the same task twice.
document.querySelectorAll("form.add").forEach((form) => {
  form.addEventListener("submit", () => {
    const button = form.querySelector("button");
    if (button) {
      button.disabled = true;
    }
  });
});
