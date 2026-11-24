// Runs inside the page under test and returns what a person could see and use on screen.
//
// The page is hostile: everything here is data to be checked by the caller, never trusted.
// This is one expression, which Playwright evaluates and whose value it returns.
(() => {
  const MAX_SCAN = 20000; // elements looked at, so a huge page cannot stall the tester
  const MAX_RAW = 1000; // entries sent back; the caller lists far fewer
  const MAX_TEXT = 400; // characters of any string sent back; the caller cuts them shorter
  const TEXT_BLOCK = 120;

  const INTERACTIVE = [
    "a[href]", "button", "input:not([type=hidden])", "select", "textarea",
    "[role=button]", "[role=link]", "[role=checkbox]", "[role=radio]", "[role=tab]",
    "[role=menuitem]", "[role=switch]", "[role=option]",
  ].join(",");
  const TEXT = "h1,h2,h3,h4,h5,h6,p,td,th";
  const SELECTOR = INTERACTIVE + ",[tabindex]," + TEXT + ",iframe,frame";
  // Elements that join the tab order only because of tabindex="0" or more.
  const isInteractive = (el) =>
    el.matches(INTERACTIVE) || (el.hasAttribute("tabindex") && el.tabIndex >= 0);
  const EXPLICIT_ROLES = new Set([
    "button", "link", "checkbox", "radio", "tab", "menuitem", "switch", "option",
  ]);
  const FIELD_ROLES = new Set([
    "checkbox", "radio", "switch", "textbox", "searchbox", "combobox", "slider",
  ]);
  const BUTTON_INPUTS = new Set(["button", "submit", "reset", "image"]);

  const width = window.innerWidth;
  const height = window.innerHeight;

  const squash = (text, max) =>
    String(text == null ? "" : text).replace(/\s+/g, " ").trim().slice(0, max);
  const textOf = (el) => squash(el.innerText !== undefined ? el.innerText : el.textContent, MAX_TEXT);

  const visible = (el, r) => {
    if (!(r.width > 0 && r.height > 0)) return false;
    if (r.right <= 0 || r.bottom <= 0 || r.left >= width || r.top >= height) return false;
    if (typeof el.checkVisibility === "function") {
      return el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });
    }
    const style = getComputedStyle(el);
    return style.display !== "none" && style.visibility !== "hidden" && style.opacity !== "0";
  };

  const inputType = (el) => (el.getAttribute("type") || "text").toLowerCase();

  const implicitRole = (el) => {
    switch (el.localName) {
      case "a": return "link";
      case "button": return "button";
      case "select": return "combobox";
      case "textarea": return "textbox";
      case "h1": case "h2": case "h3": case "h4": case "h5": case "h6": return "heading";
      case "p": return "paragraph";
      case "td": case "th": return "cell";
      case "input": {
        const type = inputType(el);
        if (BUTTON_INPUTS.has(type)) return "button";
        if (type === "checkbox" || type === "radio") return type;
        if (type === "range") return "slider";
        if (type === "search") return "searchbox";
        return "textbox";
      }
      default: return "generic";
    }
  };

  const interactiveRole = (el) => {
    const first = (el.getAttribute("role") || "").trim().toLowerCase().split(/\s+/)[0];
    if (EXPLICIT_ROLES.has(first)) return first;
    const role = implicitRole(el);
    return role === "heading" || role === "paragraph" || role === "cell" ? "generic" : role;
  };

  const kindOf = (role) => {
    if (role === "link") return "link";
    if (FIELD_ROLES.has(role)) return "field";
    return "button";
  };

  const byIds = (el) => {
    const ids = (el.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean);
    const root = el.getRootNode();
    const parts = [];
    for (const id of ids.slice(0, 10)) {
      const target = root.getElementById ? root.getElementById(id) : null;
      if (target) parts.push(textOf(target));
    }
    return squash(parts.join(" "), MAX_TEXT);
  };

  const byLabel = (el) => {
    if (!el.labels) return "";
    return squash(Array.from(el.labels).slice(0, 5).map(textOf).join(" "), MAX_TEXT);
  };

  // A link or button holding only pictures is named by their alt text.
  const imageAlts = (el) =>
    squash(
      Array.from(el.querySelectorAll("img[alt]")).slice(0, 5).map((i) => i.getAttribute("alt")).join(" "),
      MAX_TEXT,
    );

  const nameOf = (el) => {
    const attr = (name) => squash(el.getAttribute(name), MAX_TEXT);
    let own = "";
    if (el.localName === "input" && BUTTON_INPUTS.has(inputType(el)) && inputType(el) !== "image") {
      own = squash(el.value, MAX_TEXT);
    } else if (el.localName !== "input" && el.localName !== "textarea") {
      own = textOf(el) || imageAlts(el);
    }
    return (
      byIds(el) || attr("aria-label") || byLabel(el) || attr("alt") || attr("title") ||
      own || attr("placeholder")
    );
  };

  const valueOf = (el, role) => {
    const checked = (el.getAttribute("aria-checked") || "").toLowerCase();
    if (el.localName === "input") {
      const type = inputType(el);
      if (type === "password" || type === "file" || BUTTON_INPUTS.has(type)) return "";
      if (type === "checkbox" || type === "radio") return el.checked ? "checked" : "unchecked";
      return squash(el.value, MAX_TEXT);
    }
    if (el.localName === "textarea") return squash(el.value, MAX_TEXT);
    if (el.localName === "select") {
      return squash(Array.from(el.selectedOptions).map((o) => o.text).join(", "), MAX_TEXT);
    }
    if ((role === "checkbox" || role === "radio" || role === "switch") && checked) {
      return checked === "true" ? "checked" : "unchecked";
    }
    return "";
  };

  const entries = [];
  let total = 0;
  let frames = 0;
  let scanned = 0;
  let scannedAll = true;

  const visit = (root) => {
    for (const el of root.querySelectorAll("*")) {
      if (scanned++ >= MAX_SCAN) {
        scannedAll = false;
        return false;
      }
      if (el.shadowRoot && visit(el.shadowRoot) === false) return false;
      if (!el.matches(SELECTOR)) continue;
      const frame = el.localName === "iframe" || el.localName === "frame";
      if (!frame && !isInteractive(el) && !el.matches(TEXT)) continue;
      const r = el.getBoundingClientRect();
      if (!visible(el, r)) continue;
      if (frame) {
        frames += 1;
        continue;
      }
      let entry;
      if (isInteractive(el)) {
        const role = interactiveRole(el);
        entry = {
          kind: kindOf(role), role, name: nameOf(el), value: valueOf(el, role),
          x: r.left, y: r.top, w: r.width, h: r.height,
        };
      } else {
        const text = textOf(el).slice(0, TEXT_BLOCK);
        if (!text) continue;
        entry = {
          kind: "text", role: implicitRole(el), name: text, value: "",
          x: r.left, y: r.top, w: r.width, h: r.height,
        };
      }
      total += 1;
      if (entries.length < MAX_RAW) entries.push(entry);
    }
    return true;
  };

  visit(document);
  return {
    title: squash(document.title, MAX_TEXT),
    width, height, total, scanned_all: scannedAll, frames, entries,
  };
})()
